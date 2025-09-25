
import os, boto3, json
from shared.teams import post_to_teams, simple_card
from compute.handler import run as run_compute

def lambda_handler(event, context):
    env = {k: os.environ.get(k,"") for k in os.environ.keys()}
    region  = os.environ.get("AWS_REGION","us-east-1")
    webhook = os.environ["TEAMS_WEBHOOK"]
    session = boto3.Session(region_name=region)

    results = {}
  
    enable_compute = os.environ.get("ENABLE_COMPUTE","true").lower() in ("1","true","t","yes","y")
    if enable_compute:
        results["compute"] = run_compute(session, webhook, region, env)

    return {"ok": True, "modules": results}
