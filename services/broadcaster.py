async def broadcast(
    app,
    users,
    message
):

    for user in users:

        try:

            await app.bot.send_message(
                chat_id=user,
                text=message
            )

        except Exception:
            pass
