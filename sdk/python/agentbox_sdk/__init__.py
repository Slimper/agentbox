"""AgentBox Python SDK.

    from agentbox_sdk import AgentBox

    mail = AgentBox(api_key="ab_live_...", base_url="https://api.agentbox.ru")
    inbox = mail.inboxes.create(username="procurement-agent")
    mail.messages.send(inbox["id"], to=["sales@supplier.ru"], subject="Запрос КП", text="...")
    reply = mail.messages.wait_for(inbox["id"], timeout=300)
"""

from agentbox_sdk.client import AgentBox, AgentBoxError
from agentbox_sdk.webhooks import verify_webhook_signature

__all__ = ["AgentBox", "AgentBoxError", "verify_webhook_signature"]
__version__ = "0.1.0"
