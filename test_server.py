import asyncio
from telegram import Bot, InputMediaPhoto, InputFile
import io
from aiohttp import web

async def handle(request):
    text = await request.text()
    print("RECEIVED MULTIPART:", "attach://" in text, "filename=" in text)
    return web.json_response({"ok": False, "error_code": 400, "description": "Can't parse inputmedia: media not found"})

async def start_server():
    app = web.Application()
    app.router.add_post('/bot123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11/sendMediaGroup', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 8080)
    await site.start()
    return runner

async def main():
    runner = await start_server()
    bot = Bot('123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11', base_url="http://127.0.0.1:8080/bot")
    data = b'12'*100
    
    print("Testing with InputFile:")
    m1 = InputFile(io.BytesIO(data), filename='a.jpg')
    items = [InputMediaPhoto(media=m1)]
    try:
        await bot.send_media_group(123, media=items)
    except Exception as e:
        print(f"ERROR1: {type(e).__name__}: {e}")

    print("Testing with raw bytes:")
    items2 = [InputMediaPhoto(media=data)]
    try:
        await bot.send_media_group(123, media=items2)
    except Exception as e:
        print(f"ERROR2: {type(e).__name__}: {e}")

    await runner.cleanup()

if __name__ == '__main__':
    asyncio.run(main())
