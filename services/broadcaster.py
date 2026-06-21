import asyncio


async def broadcast(
    bot,
    users,
    message
):

    success = 0
    failed = 0

    for user_id in users:

        try:

            await bot.send_message(
                chat_id=user_id,
                text=message
            )

            success += 1

            await asyncio.sleep(
                0.05
            )

        except Exception:

            failed += 1

    return {
        "success": success,
        "failed": failed
    }
