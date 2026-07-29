#!/bin/bash
# ============================================================================
# sync_replica_incremental.sh
# ----------------------------------------------------------------------------
# Инкрементальное обновление ЛОКАЛЬНОЙ РЕПЛИКИ PBI-таблиц big_analytics_v5
# (localhost:5432/ad_analytics_bi на этом Mac). Запускается launchd-джобом
# (см. ~/Library/LaunchAgents/com.victory.pbireplica.sync.plist).
#
# Что делает:
#   * Тяжёлые date-таблицы (fact/arp/cookie) — ресинк ОКНА последних N дней
#     (DELETE+COPY окна параллельными чанками) — мелкая свежая дельта, быстро
#     даже на дырявом канале к Victory; захватывает и пересчёты последних дней.
#   * Остальные (Dim_*, мелкие витрины) — штатная fingerprint-сверка: не
#     менялись -> ПРОПУСК, изменились -> перезалив целиком (они мелкие, быстро).
#   * Соединения к Victory идут мимо VPN (физ.bind в скрипте).
#   * По завершении — Telegram-уведомление (успех/ошибка) из самого скрипта.
#
# НЕ incremental refresh в Power BI (он в проекте ЗАПРЕЩЁН). Это синк РЕПЛИКИ
# БД; Power BI по-прежнему Import целиком, но с быстрого localhost.
#
# Идемпотентно, безопасно для повторного запуска. Логи: $LOG.
# ============================================================================
set -uo pipefail

REPO="/Users/semen/Documents/HomeServer_PythonProject"
SCRIPT="$REPO/work/copy_pbi_tables_to_localhost.py"
# Системный python3 с psycopg2 (см. память: /usr/bin путь к CLT python3)
PY="/usr/bin/python3"
[ -x "$PY" ] || PY="$(command -v python3)"

# окно ресинка тяжёлых таблиц (дней). 7 > интервала между прогонами pipeline.
RESYNC_DAYS="${RESYNC_DAYS:-7}"
# параллельных чанков внутри тяжёлой таблицы при ресинке окна
CHUNKS="${CHUNKS:-4}"

LOGDIR="$REPO/work/big_analytics_v5/_logs"
mkdir -p "$LOGDIR"
LOG="$LOGDIR/sync_replica_$(date +%Y%m%d_%H%M%S).log"

# держим только 14 последних логов
ls -1t "$LOGDIR"/sync_replica_*.log 2>/dev/null | tail -n +15 | xargs rm -f 2>/dev/null

{
  echo "=== sync_replica_incremental START $(date '+%Y-%m-%d %H:%M:%S %z') ==="
  echo "python=$PY  delta_days=$RESYNC_DAYS  chunks=$CHUNKS"
  # --delta-days N: тяжёлые date-таблицы ресинкают окно N дней; мелкие — fingerprint.
  # --jobs 1: таблицы по очереди (внутри тяжёлой — свои CHUNKS параллельных потоков,
  #           чтобы не перегрузить канал таблицы×чанки одновременно).
  DELTA_DAYS="$RESYNC_DAYS" CHUNKS="$CHUNKS" \
    "$PY" "$SCRIPT" --delta-days "$RESYNC_DAYS" --chunks "$CHUNKS" --jobs 1
  rc=$?
  echo "=== sync_replica_incremental END rc=$rc $(date '+%Y-%m-%d %H:%M:%S %z') ==="
  exit $rc
} >> "$LOG" 2>&1
