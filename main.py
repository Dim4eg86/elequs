import os
import re
import logging
import xml.etree.ElementTree as ET
import pandas as pd
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import FSInputFile
from deep_translator import GoogleTranslator

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Инициализация бота
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Переменная окружения BOT_TOKEN не задана!")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Глобальный словарь для хранения прайса в оперативной памяти
price_dict = {}

def clean_product_name(text):
    """
    Очищает строку от количества в конце (например, ' 5', ' 3 шт', '- 2шт', ' 10шт.').
    При этом полностью сохраняет все украинские буквы, спецсимволы и знаки препинания внутри названия.
    """
    text = re.sub(r'\s*[-\u2013\u2014]*\s*\d+\s*(?:шт|шт\.|pcs)?\s*$', '', text, flags=re.IGNORECASE)
    return text.strip()

def extract_quantity(text):
    """
    Извлекает числовое значение количества из конца строки. Если не найдено, возвращает 1.
    """
    match = re.search(r'(\d+)\s*(?:шт|шт\.|pcs)?\s*$', text, flags=re.IGNORECASE)
    if match:
        return int(match.group(1))
    return 1

def load_price_list(file_path):
    """
    Парсит YML файл и загружает товары в глобальный словарь price_dict.
    """
    global price_dict
    try:
        tree = ET.parse(file_path)
        root = tree.getroot()
        
        new_price_dict = {}
        offers = root.findall(".//offer")
        
        for offer in offers:
            # Используем .// для сквозного поиска тега внутри offer
            name_elem = offer.find(".//name")
            if name_elem is not None and name_elem.text:
                name_cleaned = name_elem.text.strip().lower()
                
                # Глубокий поиск цены (.//price) и конвертация
                price_elem = offer.find(".//price")
                try:
                    price_val = float(price_elem.text) if price_elem is not None and price_elem.text else 0.0
                except (ValueError, TypeError):
                    price_val = 0.0
                
                vendor_code_elem = offer.find(".//vendorCode")
                vendor_code = vendor_code_elem.text.strip() if vendor_code_elem is not None and vendor_code_elem.text else ""
                
                new_price_dict[name_cleaned] = {
                    "id": offer.get("id", ""),
                    "price": price_val,
                    "vendorCode": vendor_code,
                    "original_name": name_elem.text.strip()
                }
        
        price_dict = new_price_dict
        logger.info(f"Успешно загружено {len(price_dict)} товаров из прайса.")
        return len(price_dict)
    except Exception as e:
        logger.error(f"Ошибка при парсинге YML: {e}")
        return None

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 Привет! Я бот проекта **Elequs**.\n\n"
        "1. Сначала пришли мне файл `price.yml` (или с любым именем `.yml`), чтобы обновить базу фурнитуры.\n"
        "2. Затем отправь мне текстовый список заказа от дизайнера (на украинском или русском языке), и я сгенерирую Excel-инвойс для 1С."
    )

@dp.message(F.document & (F.document.file_name.endswith('.yml') | F.document.file_name.endswith('.xml')))
async def handle_price_file(message: types.Message):
    msg = await message.answer("📥 Скачиваю и обновляю прайс-лист...")
    
    file_id = message.document.file_id
    file = await bot.get_file(file_id)
    file_path = file.file_path
    
    local_path = "price_list.yml"
    await bot.download_file(file_path, local_path)
    
    total_count = load_price_list(local_path)
    
    if total_count is not None:
        await msg.edit_text(f"✅ Прайс успешно обновлен! Всего товаров в базе: {total_count}")
    else:
        await msg.edit_text("❌ Произошла ошибка при разборе YML-файла. Проверьте его структуру.")

@dp.message(F.text & ~F.text.startswith('/'))
async def handle_order_list(message: types.Message):
    if not price_dict:
        await message.answer("⚠️ База товаров пуста. Сначала загрузите `price.yml` файл.")
        return
    
    status_msg = await message.answer("🔄 Переводим и сверяем список с прайсом...")
    
    lines = message.text.strip().split('\n')
    
    rows = []
    not_found = []
    
    # Инициализируем переводчик (с любого языка на украинский)
    translator = GoogleTranslator(source='auto', target='uk')
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        # Вытаскиваем количество
        quantity = extract_quantity(line)
        
        # Очищаем название от цифр/шт
        cleaned_name = clean_product_name(line)
        
        # Автоматически переводим название на украинский язык
        try:
            translated_name = translator.translate(cleaned_name)
        except Exception as e:
            logger.error(f"Ошибка перевода строки '{cleaned_name}': {e}")
            translated_name = cleaned_name  # Если упало, пробуем искать исходный текст
            
        cleaned_name_lower = translated_name.lower()
        
        # Поиск в базе (сначала точное совпадение)
        if cleaned_name_lower in price_dict:
            item = price_dict[cleaned_name_lower]
            price = item["price"]
            total_sum = price * quantity
            
            rows.append({
                "ID товара": item["id"],
                "Артикул (SKU)": item["vendorCode"],
                "Название": item["original_name"],
                "Цена": price,
                "Количество": quantity,
                "Сумма": total_sum
            })
        else:
            # ИСПРАВЛЕНО: Умный поиск по ключевым словам, если точное совпадение промахнулось
            found_match = False
            
            # Разбиваем запрос на отдельные слова (длиной > 2 символов, убирая предлоги)
            search_words = [w for w in cleaned_name_lower.split() if len(w) > 2]
            
            if search_words:
                for match_name_lower, item in price_dict.items():
                    # Считаем, сколько слов из запроса содержится в названии из прайса
                    matches_count = sum(1 for word in search_words if word in match_name_lower)
                    
                    # Считаем необходимый порог совпадений (минимум 70% слов или не меньше 2 для коротких фраз)
                    required_matches = max(2, int(len(search_words) * 0.7))
                    
                    if matches_count >= required_matches:
                        price = item["price"]
                        total_sum = price * quantity
                        rows.append({
                            "ID товара": item["id"],
                            "Артикул (SKU)": item["vendorCode"],
                            "Название": item["original_name"],
                            "Цена": price,
                            "Количество": quantity,
                            "Сумма": total_sum
                        })
                        found_match = True
                        break  # Нашли совпадение, выходим из проверки прайса для этой строки
            
            if not found_match:
                not_found.append(line)
    
    if not rows:
        await status_msg.edit_text("❌ Ни один товар из списка не был найден в прайсе. Проверьте названия.")
        return

    # Создаем DataFrame
    df = pd.DataFrame(rows)
    output_filename = f"Invoice_{message.from_user.id}.xlsx"
    
    # Генерируем Excel с автоподбором ширины колонок
    with pd.ExcelWriter(output_filename, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Invoice')
        worksheet = writer.sheets['Invoice']
        
        for col in worksheet.columns:
            max_len = max(len(str(cell.value or '')) for cell in col)
            col_letter = col[0].column_letter
            worksheet.column_dimensions[col_letter].width = max(max_len + 3, 12)

    # Формируем отчет в сообщении
    report_text = f"📊 **Инвойс успешно сформирован!**\n\n✅ Найдено позиций: {len(rows)}"
    if not_found:
        report_text += "\n\n⚠️ **Не удалось найти в прайсе:**\n" + "\n".join([f"• {item}" for item in not_found])
    
    # Отправляем файл пользователю
    excel_file = FSInputFile(output_filename)
    await message.reply_document(excel_file, caption=report_text, parse_mode="Markdown")
    
    if os.path.exists(output_filename):
        os.remove(output_filename)
        
    await status_msg.delete()

async def main():
    # Запуск бота в режиме Polling
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
