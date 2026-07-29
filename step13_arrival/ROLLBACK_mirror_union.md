# Откат MIRROR+UNION (step13 73-кол зеркало + big_analytics_unified)

Внедрено: 2026-06-05. Бэкап файлов: `big_analytics_v5/.backup_mirror_union_20260605_154108/`

## Что изменено

| Объект | Было | Стало |
|--------|------|-------|
| `big_analytics_full_arrival` (BFA) | 34 кол. (Фаза 1) | 73 кол. = зеркало `big_analytics_full` |
| `big_analytics_unified` | — (не было) | НОВАЯ физ. таблица (CTAS) = BAF ∪ BFA + `атрибуция` |
| `step13_arrival/step13.py` | 34-кол сборка | 73-кол зеркало + ad-dims Авто по key3_arrival_date |
| `step13_arrival/build_unified.py` | — | НОВЫЙ модуль сборки unified |
| `pipeline.py` / `fast_pipeline.py` | — | вызов `build_unified.run()` после campaign_status_prefix |

## DDL отката (если нужно вернуть как было)

```sql
-- 1. Удалить новую таблицу unified (PBI ШАГ 4 должен быть откатан ПЕРВЫМ —
--    репойнт партиции модели обратно на big_analytics_full).
DROP TABLE IF EXISTS public.big_analytics_unified;

-- 2. BFA: вернуть прежнюю 34-кол схему — перегенерировать старой версией step13.
--    Восстановить файл из бэкапа и прогнать step13:
--      cp .backup_mirror_union_20260605_154108/step13.py.bak step13_arrival/step13.py
--      ~/venv/bin/python3 pipeline.py --only-step=13
--    (BFA пересоздаётся через DROP+CREATE — отдельный DDL не нужен.)
```

## Откат файлов

```bash
cd work/big_analytics_v5
cp .backup_mirror_union_20260605_154108/step13.py.bak     step13_arrival/step13.py
cp .backup_mirror_union_20260605_154108/pipeline.py.bak   pipeline.py
cp .backup_mirror_union_20260605_154108/fast_pipeline.py.bak fast_pipeline.py
rm step13_arrival/build_unified.py
# затем прогнать step13 чтобы вернуть BFA к 34-кол виду
```

## Порядок отката (важно)

1. PBI: репойнт партиции модели `big_analytics_full` обратно на физ. `big_analytics_full`
   (ШАГ 4 атрибуции). Иначе DROP unified сломает модель.
2. `DROP TABLE big_analytics_unified`.
3. Восстановить файлы из бэкапа + прогнать `pipeline.py --only-step=13`.

## Идемпотентность (прямой прогон, не откат)

`build_unified.run()` и `step13.run()` — DROP+CREATE. Повторный прогон пайплайна
полностью пересоздаёт BFA и unified из текущих BAF+raw. Накопительных эффектов нет
(проверено: 3 прогона build_unified → одинаковые 3 655 961 строк).
