import os
import re
import logging
import xml.etree.ElementTree as ET
import pandas as pd
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from Levenshtein import distance

# Настройка логирования
logging.basicConfig(level=logging.INFO)

# Инициализация бота (токен заберем из переменных окружения Railway)
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Переменная окружения BOT_TOKEN не задана!")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Путь к YML-файлу (положи его в корень проекта или загрузи на сервер)
YML_FILE_PATH = "price.yml"

# Глобальный словарь для кэширования прайса в памяти
products_db = {}

def load_yml_to_memory():
    """Парсит YML файл и собирает базу товаров в оперативку"""
    global products_db
    if not os.path.exists(YML_FILE_PATH):
        logging.warning(f"Файл {YML_FILE_PATH} не найден. Сначала загрузите его!")
        return False
    
    try:
        tree = ET.parse(YML_FILE_PATH)
        root = tree.getroot()
        
        new_db = {}
        # Ищем все теги <offer> в YML
        for offer in root.findall(".//offer"):
            offer_id = offer.get("id", "")
            sku = offer.find("vendorCode").text if offer.find("vendorCode") is not None else ""
            name = offer.find("name").text if offer.find("name") is not None else ""
            price = float(offer.find("price").text) if offer.find("price") is not None else 0.0
            
            if name:
                # Ключ делаем в нижнем регистре для удобства базового поиска
                new_db[name.lower().strip()] = {
                    "id": offer_id,
                    "sku": sku,
                    "name": name,
                    "price": price
                }
        
        products_db = new_db
        logging.info(f"Успешно загружено товаров из YML: {len(products_db)}")
        return True
    except Exception as e:
        logging.error(f"Ошибка при парсинге YML: {e}")
        return False

def find_best_match(user_text_name):
    """Ищет товар по названию. Если точного совпадения нет, ищет ближайшее по расстоянию Левенштейна"""
    search_name = user_text_name.lower().strip()
    
    # 1. Прямое совпадение
    if search_name in products_db:
        return products_db[search_name]
    
    # 2. Нечеткий поиск (если опечатались)
    best_score = 999
    best_match = None
    
    for db_name, data in products_db.items():
        # Считаем разницу между строками
        dist = distance(search_name, db_name)
        # Если разница небольшая (например, до 3-4 символов в зависимости от длины)
        if dist < best_score and dist <= 4:
            best_score = dist
            best_match = data
            
    return best_match

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 Привет! Я бот проекта **Elekus**.\n\n"
        "1. Отправь мне файл `price.yml`, чтобы обновить прайс-лист товаров.\n"
        "2. Пришли мне текстовый список заказа от дизайнера, и я сделаю из него инвойс Excel для 1С."
    )

@dp.message(F.document & F.document.file_name.endswith('.yml'))
async def handle_yml_upload(message: types.Message):
    """Принимает новый YML файл и обновляет базу данных в памяти"""
    await message.answer("📥 Скачиваю и обновляю прайс-лист...")
    
    file_id = message.document.file_id
    file = await bot.get_file(file_id)
    
    # Сохраняем файл локально
    await bot.download_file(file.file_path, YML_FILE_PATH)
    
    if load_yml_to_memory():
        await message.answer(f"✅ Прайс успешно обновлен! Всего товаров в базе: {len(products_db)}")
    else:
        await message.answer("❌ Ошибка при обработке YML. Проверьте формат файла.")

@dp.message(F.text)
async def process_order_text(message: types.Message):
    """Обрабатывает текстовый список от дизайнера"""
    if not products_db:
        # Пробуем загрузить, если файл уже лежит на сервере
        if not load_yml_to_memory():
            await message.answer("⚠️ База товаров пуста. Сначала загрузите `price.yml` файл.")
            return

    lines = message.text.split("\n")
    matched_items = []
    not_found_items = []

    await message.answer("🔄 Парсим список и сверяем с прайсом...")

    for line in lines:
        if not line.strip():
            continue
            
        # Регулярка пытается отделить название от количества (поддерживает форматы: "Товар - 5", "Товар 5 шт", "Товар-5")
        match = re.search(r"(.+?)(?:[\s\-\s]*[\s\-]\s*|\s+)(\d+)\s*(?:шт|шт\.)?$", line.strip(), re.IGNORECASE)
        
        if match:
            raw_name = match.group(1).strip()
            quantity = int(match.group(2))
        else:
            # Если количество в конце строки не найдено, считаем, что количество = 1
            raw_name = line.strip()
            quantity = 1

        # Ищем товар в нашей YML-базе
        product_data = find_best_match(raw_name)
        
        if product_data:
            matched_items.append({
                "ID товара": product_data["id"],
                "Артикул (SKU)": product_data["sku"],
                "Название": product_data["name"],
                "Цена": product_data["price"],
                "Количество": quantity,
                "Сумма": product_data["price"] * quantity
            })
        else:
            not_found_items.append(line)

    if not matched_items:
        await message.answer("❌ Ни один товар из списка не был найден в прайсе. Проверьте названия.")
        return

    # Создаем Excel-файл через Pandas
    df = pd.DataFrame(matched_items)
    excel_path = f"invoice_{message.from_user.id}.xlsx"
    
    # Сохраняем в красивый xlsx формат
    df.to_excel(excel_path, index=False)

    # Отправляем инвойс пользователю
    invoice_file = types.FSInputFile(excel_path)
    
    report_text = f"📊 Инвойс успешно сформирован!\n✅ Распознано позиций: {len(matched_items)}"
    if not_found_items:
        report_text += f"\n\n⚠️ **Не удалось найти в прайсе:**\n" + "\n".join([f"• {item}" for item in not_found_items])
        
    await message.reply_document(invoice_file, caption=report_text)
    
    # Удаляем временный файл с диска
    if os.path.exists(excel_path):
        os.remove(excel_path)

async def main():
    # Предварительная загрузка базы при старте, если файл уже залит на гитхаб
    load_yml_to_memory()
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())