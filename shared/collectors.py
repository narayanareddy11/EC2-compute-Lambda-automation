
import boto3
from botocore.exceptions import ClientError

def get_acct_title(session=None):
    stss = (session or boto3.Session()).client("sts")
    try:
        aid = stss.get_caller_identity().get("Account","unknown")
    except Exception:
        aid = "unknown"
    return f"AWS {aid}"


