def speak(data, ctx):
    speaker = ctx.services.get("speech_output")
    try:
        if speaker is None:
            raise RuntimeError("Подсистема синтеза речи не запущена")
        speaker.submit(data["text"]).result()
    except Exception as exc:
        ctx.emit(
            "speech_output.result",
            {
                "action_id": ctx.action_id,
                "status": "error",
                "text": data["text"],
                "error": str(exc),
            },
        )
        return
    ctx.emit(
        "speech_output.result",
        {
            "action_id": ctx.action_id,
            "status": "success",
            "text": data["text"],
            "error": None,
        },
    )
