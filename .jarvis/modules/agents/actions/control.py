def submit(tasks):
    """Вернуть быстрый dispatcher, передающий action фоновому handler."""

    return tasks.submit
