from agentbox.jobs.worker import Handler


def default_handlers() -> dict[str, Handler]:
    """Core job handlers plus those registered by installed extensions."""
    from agentbox.domains.verify import verify_domain
    from agentbox.extensions import registry
    from agentbox.inbound.processor import process_inbound
    from agentbox.lifecycle.expire import expire_inbox
    from agentbox.outbound.sender import send_outbound
    from agentbox.usage.rollup import rollup_usage
    from agentbox.webhooks.delivery import deliver_webhooks

    handlers: dict[str, Handler] = {"inbox_expire": expire_inbox, "webhook_deliver": deliver_webhooks,
                                    "outbound_send": send_outbound, "inbound_process": process_inbound,
                                    "domain_verify": verify_domain, "usage_rollup": rollup_usage}
    handlers.update(registry().job_handlers())
    return handlers
