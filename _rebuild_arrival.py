import sys, time, importlib
sys.path.insert(0, "/home/semen_vi/big_analytics_v5")
import config.db as db_module

RUN_ID = "rebuild_arrival_manual"

def log(m):
    print(time.strftime("%H:%M:%S") + " " + str(m), flush=True)

db_module.init_pool()
conn = db_module.get_conn()
# КОРЕНЬ ЗАВИСАНИЯ: parallel tuple-queue deadlock (workers зависли в IPC/MessageQueueSend 2ч)
# при 6 воркерах ПОД КОНТЕНШЕНОМ (внешние SELECT по arrival держали блокировки/буферы).
# Сейчас внешних читателей нет -> даём ОГРАНИЧЕННЫЙ параллелизм (2 воркера): быстрее
# single-thread, но мало воркеров на пустой системе не воспроизводит IPC-deadlock.
with conn.cursor() as cur:
    cur.execute("SET max_parallel_workers_per_gather = 2")
    cur.execute("SET statement_timeout = '20min'")
conn.commit()
log("параллелизм=2 воркера, statement_timeout=20min")

log("=== step13.run (DROP+CREATE big_analytics_full_arrival) ===")
t = time.perf_counter()
s13 = importlib.import_module("step13_arrival.step13")
r13 = s13.run(conn, run_id=RUN_ID)
log("step13 DONE за %.1fс -> %s" % (time.perf_counter() - t, r13))

log("=== build_unified.run (big_analytics_unified = full union arrival) ===")
t = time.perf_counter()
bu = importlib.import_module("step13_arrival.build_unified")
rbu = bu.run(conn, run_id=RUN_ID)
log("build_unified DONE за %.1fс -> %s" % (time.perf_counter() - t, rbu))

db_module.put_conn(conn)
log("=== ALL DONE ===")
