from fastapi import APIRouter
from agent.graph import agent
from agent.tools.slack import send_message

router = APIRouter()

@router.post("/slack/command")
async def slack_command(payload: dict):
    channel = payload["channel_id"]
    question = payload["text"]

    result = agent.invoke({"question": question})

    response = f"""
*Diagnosis:* {result['diagnosis']}
*Confidence:* {result['confidence']}
*Evidence:* {', '.join(result.get('evidence', []))}
"""

    send_message(channel, response)
    return {"ok": True}
