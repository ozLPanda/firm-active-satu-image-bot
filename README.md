# Satu Image Upload Bot

GUI-бот для загрузки изображений в товары на `my.satu.kz`.

## Как работает

1. Пользователь выбирает папку с изображениями.
2. Пользователь выбирает Excel-файл с эталонными артикулами и названиями.
3. Бот берет артикул из имени файла изображения без расширения.
4. Бот ищет товар на `my.satu.kz`, открывает карточку, проверяет артикул и похожесть названия.
5. Если изображений нет, бот загружает фото.
6. Если изображения уже есть, бот откладывает товар в отдельный список и после прохода спрашивает, что делать.

Поддерживаемые расширения: `.jpg`, `.jpeg`, `.png`, `.gif`, `.webp`.

## Запуск из исходников

```powershell
cd C:\Users\admin\Desktop\active-firm\satu_image_bot
python -m pip install -r requirements.txt
python -m playwright install chromium
python .\app.py
```

При первом запуске Chromium откроется с постоянным профилем из папки `%LOCALAPPDATA%\SatuImageBot\profile`.
Войдите в аккаунт satu.kz вручную. Следующие запуски будут использовать сохраненную сессию.
Эта папка не удаляется при пересборке приложения, поэтому авторизация должна сохраняться между версиями `.exe`.

## Сборка EXE

```powershell
cd C:\Users\admin\Desktop\active-firm\satu_image_bot
.\build.bat
```

Готовый файл:

```text
C:\Users\admin\Desktop\active-firm\satu_image_bot\dist\satu-image-bot\satu-image-bot.exe
```

## Отчеты

Отчеты сохраняются в `%LOCALAPPDATA%\SatuImageBot\reports`:

- `upload-report-YYYYMMDD-HHMMSS.csv`
- `upload-report-YYYYMMDD-HHMMSS.json`

Возможные статусы: `uploaded`, `skipped_existing_image`, `replaced_existing_image`, `skipped_by_user`, `not_found`, `name_mismatch`, `invalid_file`, `upload_error`, `stopped`.

## Настройка DOM-селекторов

Разметка админки satu.kz может изменяться. Все селекторы собраны в `bot.py` в классе `Selectors`.
Если при первом реальном прогоне бот не найдет ссылку товара, поле артикула, название или загрузчик изображения, нужно поправить соответствующий список селекторов.
