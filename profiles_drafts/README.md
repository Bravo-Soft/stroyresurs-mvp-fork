# Черновики профилей переписи (census)

Сюда `site_profiles/census_collector.py` пишет черновики `<домен>.yaml` по итогам
обработки компании в census-режиме (`config.census_enabled = true`). Черновики
НЕ применяются пайплайном — только файлы из `mvp/profiles/`.

Принятие черновика (пост-обработка переписи, не рантайм):
`confidence >= config.profile_accept_confidence (0.8)` И `product_cards > 0` И
медиана ТХ >= 3 → перенос в `mvp/profiles/`; иначе — строка в `_review_queue.md`
(домен, причина, ссылки на артефакты) для разбора через quality-loop.
