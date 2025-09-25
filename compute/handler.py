# ec2_compute/handler.py
import os, re, boto3, math, logging
from datetime import datetime, timedelta, timezone
from shared.teams import post_to_teams, simple_card
from shared.collectors import get_acct_title

# ---------------- logging ----------------
logger = logging.getLogger(__name__)

# ---------------- UI helpers ----------------
def _cell(text, *, bold=False, color=None, width="auto", wrap=False):
    block = {"type":"TextBlock","text":str(text),"wrap":bool(wrap),
             "maxLines":1 if not wrap else 0,"size":"Small","spacing":"Small"}
    if bold:  block["weight"]="Bolder"
    if color: block["color"]=color
    return {"type":"Column","width":width,"items":[block]}

def _color_for(pct, warn, alert):
    if pct is None: return None
    try: v = float(pct)
    except Exception: return None
    if v >= alert: return "attention"
    if v >= warn:  return "warning"
    return "good"

def _emoji_for(pct, warn, alert):
    if pct is None: return ""
    try: v = float(pct)
    except Exception: return ""
    if v >= alert: return "🔴"
    if v >= warn:  return "🟡"
    return "🟢"

def _fmt_pct(v):
    return "N/A" if v is None else f"{float(v):.0f}%"

# ---------------- CloudWatch/EC2 helpers ----------------
def _get_instances(ec2, tag_key=None, tag_val=None, only_running=True, max_instances=200):
    filters=[{"Name":"instance-state-name","Values":["running"]}] if only_running else []
    if tag_key and tag_val:
        filters.append({"Name": f"tag:{tag_key}", "Values": [tag_val]})
    res=[]
    try:
        paginator = ec2.get_paginator("describe_instances")
        for page in paginator.paginate(Filters=filters) if filters else paginator.paginate():
            for r in page.get("Reservations", []):
                for i in r.get("Instances", []):
                    res.append(i)
                    if len(res) >= max_instances:
                        return res
    except Exception:
        pass
    return res

def _latest_stat_cw(cw, namespace, metric_name, dims, minutes=15, period=300, stat="Average", *, start=None, end=None):
    try:
        end = end or datetime.now(timezone.utc)
        start = start or (end - timedelta(minutes=minutes))
        dps = cw.get_metric_statistics(
            Namespace=namespace, MetricName=metric_name, Dimensions=dims,
            StartTime=start, EndTime=end, Period=period, Statistics=[stat]
        ).get("Datapoints", [])
        if not dps: return None
        latest = max(dps, key=lambda x: x["Timestamp"])
        return latest.get(stat)
    except Exception:
        return None

def _series_stat_cw(cw, namespace, metric_name, dims, *, start, end, period, stat="Average"):
    try:
        dps = cw.get_metric_statistics(
            Namespace=namespace, MetricName=metric_name, Dimensions=dims,
            StartTime=start, EndTime=end, Period=period, Statistics=[stat]
        ).get("Datapoints", [])
        dps.sort(key=lambda x: x["Timestamp"])  # ascending
        out=[]
        for dp in dps:
            ts = dp["Timestamp"]
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            else:
                ts = ts.astimezone(timezone.utc)
            out.append((ts, dp.get(stat)))
        return out
    except Exception:
        return []

def _find_cwagent_metric_dims(cw, metric_name, instance_id, max_scan=25):
    try:
        paginator = cw.get_paginator("list_metrics")
        dims = []; count = 0
        for page in paginator.paginate(Namespace="CWAgent", MetricName=metric_name, Dimensions=[{"Name":"InstanceId"}]):
            for m in page.get("Metrics", []):
                if any(d.get("Name")=="InstanceId" and d.get("Value")==instance_id for d in m.get("Dimensions", [])):
                    dims.append(m.get("Dimensions", [])); count += 1
                    if count >= max_scan: return dims
        return dims
    except Exception:
        return []

def _series_max_cwagent(cw, metric_name, dims_list, *, start, end, period):
    buckets = {}
    for dims in dims_list or []:
        series = _series_stat_cw(cw, "CWAgent", metric_name, dims, start=start, end=end, period=period, stat="Average")
        for ts, val in series:
            if val is None:
                continue
            k = ts.replace(microsecond=0)
            buckets[k] = max(buckets.get(k, float("-inf")), float(val))
    out = [(ts, v) for ts, v in sorted(buckets.items()) if v != float("-inf")]
    return out

def _max_across_dims(cw, metric_name, dims_list, minutes=15, period=300, *, start=None, end=None):
    best = None
    for dims in dims_list:
        val = _latest_stat_cw(cw, "CWAgent", metric_name, dims, minutes=minutes, period=period, stat="Average", start=start, end=end)
        if val is None: continue
        best = val if best is None else max(best, val)
    return best

def _ec2_console_link(region, instance_id):
    return f"https://{region}.console.aws.amazon.com/ec2/home?region={region}#InstanceDetails:instanceId={instance_id}"

# ---------------- Severity helpers ----------------
def _metric_level(v, warn, alert):
    try:
        if v is None: return "OK"  # missing metric shouldn't trigger
        v = float(v)
    except Exception:
        return "OK"
    if v >= alert: return "ALERT"
    if v >= warn:  return "WARN"
    return "OK"

def _row_overall_level(cpu, mem, dsk, th):
    levels = [
        _metric_level(cpu, th["CPU_WARN"],  th["CPU_ALERT"]),
        _metric_level(mem, th["MEM_WARN"],  th["MEM_ALERT"]),
        _metric_level(dsk, th["DISK_WARN"], th["DISK_ALERT"]),
    ]
    if "ALERT" in levels: return "ALERT"
    if "WARN"  in levels: return "WARN"
    return "OK"

# ---------------- Card builder ----------------
def _build_cards(account_label, rows, rows_per_card):
    if not rows:
        return [simple_card(f"{account_label} - Compute (CPU/Mem/Disk)", "No WARN/ALERT instances.")]
    headers = ["Instance-ID / Name", "CPU", "Mem", "Disk"]
    widths  = [8, 2, 2, 2]
    cards=[]
    for i in range(0, len(rows), rows_per_card):
        chunk = rows[i:i+rows_per_card]
        body = [
            {"type":"TextBlock","text":f"{account_label} - Compute (CPU/Mem/Disk) - ⚠️ Offenders","weight":"Bolder","size":"Medium"},
            {"type":"ColumnSet","separator":True,"spacing":"Medium",
             "columns":[_cell(h, bold=True, width=str(widths[j])) for j,h in enumerate(headers)]}
        ]
        for iid, name, cpu, mem, disk, th, region in chunk:
            link = _ec2_console_link(region, iid)
            id_cell = f"[{iid}]({link})" + (f"\n{name}" if name else "")
            cpu_cell = f"{_emoji_for(cpu, th['CPU_WARN'], th['CPU_ALERT'])} {_fmt_pct(cpu)}"
            mem_cell = f"{_emoji_for(mem, th['MEM_WARN'], th['MEM_ALERT'])} {_fmt_pct(mem)}"
            dsk_cell = f"{_emoji_for(disk, th['DISK_WARN'], th['DISK_ALERT'])} {_fmt_pct(disk)}"
            body.append({"type":"ColumnSet","columns":[
                _cell(id_cell, width=str(widths[0]), wrap=True),
                _cell(cpu_cell, width=str(widths[1]), color=_color_for(cpu, th['CPU_WARN'], th['CPU_ALERT'])),
                _cell(mem_cell, width=str(widths[2]), color=_color_for(mem, th['MEM_WARN'], th['MEM_ALERT'])),
                _cell(dsk_cell, width=str(widths[3]), color=_color_for(disk, th['DISK_WARN'], th['DISK_ALERT'])),
            ]})
        cards.append({
            "type":"message",
            "attachments":[{"contentType":"application/vnd.microsoft.card.adaptive",
                            "content":{"$schema":"http://adaptivecards.io/schemas/adaptive-card.json",
                                       "type":"AdaptiveCard","version":"1.4","body":body}}]
        })
    return cards

# ---------------- Email build/send ----------------
def _build_email(acct, rows):
    def row_html(r):
        iid, name, cpu, mem, dsk, th, region = r
        link = _ec2_console_link(region, iid)
        cpu_s = f"{_emoji_for(cpu, th['CPU_WARN'], th['CPU_ALERT'])} {_fmt_pct(cpu)}"
        mem_s = f"{_emoji_for(mem, th['MEM_WARN'], th['MEM_ALERT'])} {_fmt_pct(mem)}"
        dsk_s = f"{_emoji_for(dsk, th['DISK_WARN'], th['DISK_ALERT'])} {_fmt_pct(dsk)}"
        name_html = f"<br/><span style='color:#555'>{name}</span>" if name else ""
        return f"<tr><td><a href='{link}'>{iid}</a>{name_html}</td><td style='text-align:right'>{cpu_s}</td><td style='text-align:right'>{mem_s}</td><td style='text-align:right'>{dsk_s}</td></tr>"

    title = f"{acct} - EC2 Utilization Alerts (CPU/Mem/Disk)"
    if not rows:
        html = f"<html><body><h3>{title}</h3><p>No WARN/ALERT instances.</p></body></html>"
        text = f"{title}\nNo WARN/ALERT instances.\n"
        return text, html

    header = """
    <style>
    table { border-collapse: collapse; width: 100%; font-family: Arial, sans-serif; font-size: 13px; }
    th, td { border: 1px solid #ddd; padding: 6px 8px; }
    th { background: #f5f5f5; text-align: left; }
    </style>"""
    rows_html = "\n".join(row_html(r) for r in rows)
    html = f"""<html><head>{header}</head><body>
    <h3>{title}</h3>
    <p>Only instances breaching <b>WARN</b>/<b>ALERT</b> thresholds are listed below.</p>
    <table>
      <tr><th>Instance (link) / Name</th><th style='text-align:right'>CPU</th><th style='text-align:right'>Mem</th><th style='text-align:right'>Disk</th></tr>
      {rows_html}
    </table>
    <p style="color:#777">Tip: Install CloudWatch Agent to populate Mem/Disk metrics.</p>
    </body></html>"""

    def row_text(r):
        iid, name, cpu, mem, dsk, th, region = r
        link = _ec2_console_link(region, iid)
        name_s = f" {name}" if name else ""
        return f"{iid}{name_s}\n  CPU={_fmt_pct(cpu)}  MEM={_fmt_pct(mem)}  DISK={_fmt_pct(dsk)}\n  {link}\n"
    text = f"{title}\n\n" + "\n".join(row_text(r) for r in rows)
    return text, html

def _parse_email_list(raw: str):
    """
    Accepts comma/semicolon/space/newline separated addresses, e.g.:
      "devops@x.com, sre@x.com; owner@x.com teamlead@x.com"
    Returns a de-duplicated list preserving order.
    """
    if not raw: return []
    parts = re.split(r'[,\s;]+', raw.strip())
    parts = [p for p in parts if p]
    seen = set(); out = []
    for p in parts:
        key = p.lower()
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out

def _send_email_ses(session, region, subject, text_body, html_body, env):
    if env.get("ENABLE_MAIL_REPORT","true").strip().lower() not in ("1","true","t","yes","y"):
        return None
    mail_from = (env.get("MAIL_FROM","") or "").strip()
    to_list   = _parse_email_list(env.get("MAIL_TO",""))
    cc_list   = _parse_email_list(env.get("MAIL_CC",""))
    bcc_list  = _parse_email_list(env.get("MAIL_BCC",""))
    if not mail_from or not to_list:
        return None
    ses = session.client("ses", region_name=region)
    dest = {"ToAddresses": to_list}
    if cc_list:  dest["CcAddresses"]  = cc_list
    if bcc_list: dest["BccAddresses"] = bcc_list
    return ses.send_email(
        Source=mail_from,
        Destination=dest,
        Message={
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body": {
                "Text": {"Data": text_body, "Charset": "UTF-8"},
                "Html":  {"Data": html_body,  "Charset": "UTF-8"},
            },
        },
    )

# ---------------- Public entry ----------------
def run(session, webhook, region, env):
    logger.setLevel(getattr(logging, (env.get("LOG_LEVEL","INFO") or "INFO").upper(), logging.INFO))

    acct = get_acct_title(session)
    ec2 = session.client("ec2", region_name=region)
    cw  = session.client("cloudwatch", region_name=region)

    enable = env.get("ENABLE_EC2_UTILIZATION", "true").strip().lower() in ("1","true","t","yes","y")
    if not enable:
        logger.info("EC2 Utilization check skipped: ENABLE_EC2_UTILIZATION=false")
        return {"ok": True, "instances": 0, "skipped": "ENABLE_EC2_UTILIZATION=false"}

    minutes  = int(env.get("WINDOW_MIN", "10"))       # 10 for 10-min lookback
    period   = int(env.get("PERIOD", "60"))           # 60 for 1-min buckets
    rows_per_card = int(env.get("ROWS_PER_CARD","20"))
    max_instances = int(env.get("MAX_INSTANCES","200"))
    only_running  = True
    log_series    = env.get("LOG_1MIN_SERIES","true").strip().lower() in ("1","true","t","yes","y")

    end_utc   = datetime.now(timezone.utc)
    start_utc = end_utc - timedelta(minutes=minutes)
    buckets   = int(math.ceil((minutes * 60) / period))
    logger.info(
        "EC2 metrics window: WINDOW_MIN=%s, PERIOD=%ss, buckets≈%s, start=%s, end=%s",
        minutes, period, buckets,
        start_utc.isoformat(timespec="seconds"),
        end_utc.isoformat(timespec="seconds"),
    )

    thr = {
        "CPU_WARN":  float(env.get("CPU_WARN",  "70")),
        "CPU_ALERT": float(env.get("CPU_ALERT", "90")),
        "MEM_WARN":  float(env.get("MEM_WARN",  "70")),
        "MEM_ALERT": float(env.get("MEM_ALERT", "90")),
        "DISK_WARN": float(env.get("DISK_WARN", "80")),
        "DISK_ALERT":float(env.get("DISK_ALERT","90")),
    }

    tag_key  = env.get("INSTANCE_TAG_KEY",  "").strip() or None
    tag_val  = env.get("INSTANCE_TAG_VALUE","").strip() or None

    instances = _get_instances(ec2, tag_key, tag_val, only_running=only_running, max_instances=max_instances)
    if not instances:
        logger.info("No running EC2 instances found for tag filter key=%r value=%r", tag_key, tag_val)
        # Do NOT send Teams/Email when nothing to report
        return {"ok": True, "instances": 0}

    rows_all=[]
    for inst in instances:
        iid  = inst.get("InstanceId","-")
        name = next((t["Value"] for t in inst.get("Tags",[]) if t.get("Key")=="Name"), "")
        cpu  = _latest_stat_cw(cw, "AWS/EC2", "CPUUtilization", [{"Name":"InstanceId","Value":iid}],
                               minutes=minutes, period=period, stat="Average", start=start_utc, end=end_utc)
        meml = _find_cwagent_metric_dims(cw, "mem_used_percent",  iid)
        mem  = _max_across_dims(cw, "mem_used_percent",  meml, minutes=minutes, period=period, start=start_utc, end=end_utc) if meml else None
        dskl = _find_cwagent_metric_dims(cw, "disk_used_percent", iid)
        dsk  = _max_across_dims(cw, "disk_used_percent", dskl, minutes=minutes, period=period, start=start_utc, end=end_utc) if dskl else None

        if log_series:
            cpu_series = _series_stat_cw(cw, "AWS/EC2", "CPUUtilization", [{"Name":"InstanceId","Value":iid}],
                                         start=start_utc, end=end_utc, period=period, stat="Average")
            mem_series = _series_max_cwagent(cw, "mem_used_percent",  meml, start=start_utc, end=end_utc, period=period) if meml else []
            dsk_series = _series_max_cwagent(cw, "disk_used_percent", dskl, start=start_utc, end=end_utc, period=period) if dskl else []

            def _fmt_series(s):
                return ", ".join(f"{ts.strftime('%H:%M')}={float(v):.0f}%" for ts, v in s if v is not None)

            logger.info("Instance %s (%s) 1-min CPU series [%d pts]: %s", iid, name or "-", len(cpu_series), _fmt_series(cpu_series) or "no datapoints")
            logger.info("Instance %s (%s) 1-min MEM series [%d pts]: %s", iid, name or "-", len(mem_series), _fmt_series(mem_series) or "no datapoints")
            logger.info("Instance %s (%s) 1-min DISK series[%d pts]: %s", iid, name or "-", len(dsk_series), _fmt_series(dsk_series) or "no datapoints")

        rows_all.append((iid, name, cpu, mem, dsk, thr, region))

    # Filter ONLY offenders (WARN/ALERT on any metric)
    offenders = []
    for row in rows_all:
        iid, name, cpu, mem, dsk, th, region = row
        level = _row_overall_level(cpu, mem, dsk, th)
        if level in ("WARN", "ALERT"):
            offenders.append(row)

    if not offenders:
        logger.info("No WARN/ALERT instances. Skipping Teams and Email.")
        return {"ok": True, "instances": len(rows_all), "alerts_sent": 0}

    # 1) Teams — only offenders
    for card in _build_cards(acct, offenders, rows_per_card):
        post_to_teams(webhook or os.environ.get("TEAMS_WEBHOOK",""), card)

    # 2) Email — only offenders
    subject_default = f"EC2 Utilization Alerts - {len(offenders)} instance(s)"
    text_body, html_body = _build_email(acct, offenders)
    resp = _send_email_ses(session, region, env.get("MAIL_SUBJECT", subject_default), text_body, html_body, env)
    if resp:
        logger.info("Emailed EC2 utilization alerts. Offenders=%d", len(offenders))
    else:
        logger.info("Email skipped (missing MAIL_FROM/MAIL_TO or ENABLE_MAIL_REPORT=false)")

    return {"ok": True, "instances": len(rows_all), "alerts_sent": len(offenders)}
