import asyncio, io
from telegram import Bot, InputMediaPhoto, InputFile

async def main():
    bot = Bot('123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11')
    data = b'12'*100
    m1 = InputFile(io.BytesIO(data), filename='a.jpg')
    m2 = InputFile(io.BytesIO(data), filename='b.jpg')
    items = [InputMediaPhoto(media=m1), InputMediaPhoto(media=m2)]
    try:
        await bot.send_media_group(123, media=items)
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")

if __name__ == '__main__':
    asyncio.run(main())
