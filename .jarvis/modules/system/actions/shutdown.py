def shutdown(data, ctx):
    ctx.emit(
        "system.shutdown_requested",
        {"action_id": ctx.action_id, "reason": data["reason"]},
    )
    request = ctx.services.get("shutdown_request")
    if request is None:
        raise RuntimeError("Сервис штатного завершения не зарегистрирован")
    request.set()
