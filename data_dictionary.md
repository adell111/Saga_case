# Data dictionary — кейс 2 «Кредитный конвейер»

## Как заполнять

В `applications.csv` 14 колонок. Все нужно описать. Одна строка — образец.
Для каждой колонки укажи: тип, обязательность, ограничение, пример (из CSV), код ошибки (если валидируется), комментарий.

Обозначения:

- **Обяз.** — `да` (клиент обязан передать), `да (сервер)` (генерирует/вычисляет сервис), `условно` (зависит от решения), `нет` (необязательно).
- **Код ошибки** —  коды с пометкой `*` используются только валидатором и в CSV не встречаются (в данных нет таких строк)


## Образец (не трогать)

|Поле|Тип|Обяз.|Ограничение|Пример|Код ошибки|Комментарий|
|-|-|-|-|-|-|-|
|app\_id|string|да (сервер)|`app\_NNNN`|app\_0001|—|генерируется сервисом|

## Ваша таблица — 13 оставшихся колонок

|Поле|Тип|Обяз.|Ограничение|Пример|Код ошибки|Комментарий|
|-|-|-|-|-|-|-|
|age|integer|да|18-70|25|AGE_OUT_OF_RANGE; MISSING_AGE*|Возрастной ценз продукта|
|income|number|да|> 0, ₽/мес; порог отказа < 30 000|120000|MISSING_INCOME; LOW_INCOME|База скоринга,DTI|
|employment|enum|да|employedё / self-employed / unemployed /student / retired|employed|INVALID_EMPLOYMENT|Стабильность дохода. unemployed = немедленный отказ|
|family|enum|да|single / married / children|married|INVALID_FAMILY*|Финансковая нагрузка домохозяйства|
|amount|integer|да|10 000 - 5 000 000 ₽; доп. лимит: amount ≤ 10 × income|50000|AMOUNT_LIMIT; AMOUNT_OUT_OF_RANGE*|Тело кредита. Проверяется |
|term|number|да|3-60 мес.|31|TERM_OUT_OF_RANGE*|Срок кредита --> множитель аннуитета.|
|decision|enum|да (сервер)|APPROVED/ REFUSED/ NEED_MORE_DATA/ MANUAL_REVIEW|APPROVED|—|Итог конвейера. Вычисляется. Пользователем не изменяется.|
|reason|enum|условно|"" если 'APPROVED', иначе ровно 1 код ошибки из 08_reasonds.md|AMOUNT_LIMIT|—|Объяснение решения. У каждого не 'APPROVED' есть причина.|
|client\_id|string|?|?|?|?|?|
|passport\_hash|?|?|?|?|?|?|
|dti|number|?|?|?|?|?|
|bki\_score|?|?|?|?|?|?|
|smev\_status|enum|?|MATCH / MISMATCH / TIMEOUT / NOT_CALLED|?|?|?|

## Критерий приёмки

* \[ ] Все 14 колонок описаны
* \[ ] Для age указан диапазон 18–70 и AGE\_OUT\_OF\_RANGE
* \[ ] Для income — > 0 и MISSING\_INCOME
* \[ ] Для employment — справочник из 5 значений и INVALID\_EMPLOYMENT
* \[ ] Для passport\_hash — только SHA-256, оригинал не хранится
* \[ ] Для dti — формула (аннуитет / доход), порог 0.5
* \[ ] Для bki\_score — диапазон 300–850, < 550 = отказ
* \[ ] Для smev\_status — MATCH / MISMATCH / TIMEOUT / NOT\_CALLED
* \[ ] Комментарий объясняет «зачем поле»

## Подсказки

* employment — enum из 5: employed, self-employed, student, retired, unemployed.
* family — enum из 3: single, married, children.
* decision — enum из 4: APPROVED, REFUSED, NEED\_MORE\_DATA, MANUAL\_REVIEW.
* passport\_hash — 16 hex-символов, не оригинал паспорта.
* dti — число с 2 знаками, от 0 до бесконечности, порог 0.5.

