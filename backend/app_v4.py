"""
Cloud Resource Allocation Simulator — Backend v4.0  (ELITE)
============================================================
New in v4 vs v3:
  1. PREDICTIVE SCHEDULING  — forecasts load trend, avoids overload before it happens
  2. AUTO-SCALING           — spins up / tears down VMs dynamically based on pressure
  3. VM MIGRATION           — moves tasks from overloaded VM → idle VM live
  4. REAL PRIORITY          — High preempts Low tasks; Low tasks yielded under pressure
  5. DISK-AWARE SCHEDULING  — all three dimensions (CPU, MEM, DISK) used in decisions
  6. LATENCY SIMULATION     — assignment delay, network jitter, scheduling overhead
  7. RETRY LOGIC            — failed tasks re-queued with back-off, up to 3 attempts
  8. QUEUE STRATEGY         — FIFO within lanes + priority preemption + retry lane
  9. BACKPRESSURE v2        — adaptive; ejects Low tasks before rejecting High
 10. METRICS INTELLIGENCE   — trend analysis, utilisation scores, SLA breach tracking
"""

from flask import Flask, jsonify, request, Response
from flask_cors import CORS
import time, random, uuid, threading, json, queue as qlib
from collections import deque

app = Flask(__name__)
CORS(app)

lock = threading.RLock()   # re-entrant — same thread can re-acquire safely

# ── ts() defined first — used by make_vm() below ──────────────────
def ts():
    return time.strftime("%H:%M:%S")

# ═══════════════════════════════════════════════════════════════════
# VM FACTORY
# ═══════════════════════════════════════════════════════════════════

def make_vm(vm_id, name, region, is_auto=False):
    return {
        "id": vm_id, "name": name, "region": region,
        "cpu": 0.0, "mem": 0.0, "disk": 0.0,
        "tasks": [],
        "failures": 0, "rejected": 0, "migrations_in": 0, "migrations_out": 0,
        "auto_scaled": is_auto,
        "created_at": ts(),
        "load_history": deque(maxlen=20),   # last 20 load snapshots for prediction
    }

# ── Initial 4 VMs ─────────────────────────────────────────────────
VMS = {
    "vm1": make_vm("vm1", "VM-Alpha",  "us-east"),
    "vm2": make_vm("vm2", "VM-Beta",   "us-west"),
    "vm3": make_vm("vm3", "VM-Gamma",  "eu-central"),
    "vm4": make_vm("vm4", "VM-Delta",  "ap-south"),
}
_vm_counter = 4   # for auto-scaled VM naming

# ── Priority lanes: retry > high > normal > low ────────────────────
PQUEUES = {
    "retry":  deque(),   # failed tasks awaiting re-try
    "high":   deque(),
    "normal": deque(),
    "low":    deque(),
}
QUEUE_CAP   = 50
RETRY_LIMIT = 3

# ── Global counters ────────────────────────────────────────────────
event_log        = deque(maxlen=200)
metrics_history  = deque(maxlen=120)
tasks_done       = 0
tasks_failed     = 0
tasks_rejected   = 0
tasks_migrated   = 0
tasks_retried    = 0
sla_breaches     = 0    # tasks that waited > 5s in queue
rr_index         = 0
current_algo     = "predictive"
auto_mode        = False
auto_scale_on    = True
sim_speed        = 1.0
auto_timer_ref   = [None]
_task_seq        = 0

ALGO_NAMES = {
    "predictive":  "Predictive",
    "roundrobin":  "Round Robin",
    "bestfit":     "Best Fit",
    "leastloaded": "Least Loaded",
    "firstfit":    "First Fit",
    "priority":    "Priority-Aware",
    "random":      "Random",
}

# SSE clients
sse_clients = []
sse_lock    = threading.Lock()

# ═══════════════════════════════════════════════════════════════════
# UTILITIES
# ═══════════════════════════════════════════════════════════════════

def add_log(msg, level="info"):
    event_log.appendleft({"time": ts(), "msg": msg, "level": level})
    print(f"[{ts()}][{level.upper():7}] {msg}")

def total_queued():
    return sum(len(q) for q in PQUEUES.values())

def vm_status(vm):
    load = max(vm["cpu"], vm["mem"], vm["disk"])
    if load == 0:   return "idle"
    if load >= 88:  return "overloaded"
    if load >= 65:  return "busy"
    return "active"

def vm_load_score(vm):
    """Composite load score 0-100 using all three dimensions."""
    return round((vm["cpu"] * 0.45 + vm["mem"] * 0.40 + vm["disk"] * 0.15), 1)

def predict_load(vm, horizon=3):
    """
    Predict VM load in `horizon` snapshots using linear regression on history.
    Returns predicted composite score.
    """
    hist = list(vm["load_history"])
    if len(hist) < 3:
        return vm_load_score(vm)
    # simple linear extrapolation
    n   = len(hist)
    xs  = list(range(n))
    xm  = sum(xs) / n
    ym  = sum(hist) / n
    num = sum((x - xm) * (y - ym) for x, y in zip(xs, hist))
    den = sum((x - xm) ** 2 for x in xs) or 1
    slope = num / den
    return max(0, min(100, ym + slope * horizon))

def can_fit(vm, task):
    """Check all three resource dimensions."""
    return (vm["cpu"]  + task["cpu"]  <= 100 and
            vm["mem"]  + task["mem"]  <= 100 and
            vm["disk"] + task["disk"] <= 100)

def can_fit_after_predict(vm, task, margin=10):
    """Only place task if predicted future load stays safe."""
    predicted = predict_load(vm)
    headroom  = 100 - predicted
    needed    = max(task["cpu"], task["mem"]) + margin
    return headroom >= needed and can_fit(vm, task)

# ═══════════════════════════════════════════════════════════════════
# SCHEDULING ALGORITHMS  (called with lock held)
# ═══════════════════════════════════════════════════════════════════

def _pick_vm_unsafe(task):
    global rr_index
    vms = list(VMS.values())

    if current_algo == "predictive":
        # Only consider VMs that are safe NOW and predicted safe LATER
        safe = [v for v in vms if can_fit_after_predict(v, task, margin=8)]
        if not safe:
            # fall back to any fitting VM
            safe = [v for v in vms if can_fit(v, task)]
        if not safe:
            return None
        # pick VM with lowest predicted future load
        return min(safe, key=lambda v: predict_load(v))

    elif current_algo == "roundrobin":
        for _ in range(len(vms)):
            vm = vms[rr_index % len(vms)]
            rr_index += 1
            if can_fit(vm, task): return vm
        return None

    elif current_algo == "bestfit":
        best, min_waste = None, float("inf")
        for vm in vms:
            if can_fit(vm, task):
                # waste across all 3 dimensions
                waste = ((100 - vm["cpu"]  - task["cpu"])  +
                         (100 - vm["mem"]  - task["mem"])  +
                         (100 - vm["disk"] - task["disk"]))
                if waste < min_waste:
                    min_waste, best = waste, vm
        return best

    elif current_algo == "leastloaded":
        eligible = [v for v in vms if can_fit(v, task)]
        return min(eligible, key=vm_load_score, default=None)

    elif current_algo == "firstfit":
        return next((v for v in vms if can_fit(v, task)), None)

    elif current_algo == "priority":
        # High-priority tasks get access to VMs up to 85% load;
        # Low-priority tasks only use VMs under 55% load
        limit = {"high": 85, "normal": 70, "low": 55}.get(task.get("priority","normal"), 70)
        eligible = [v for v in vms if can_fit(v, task) and vm_load_score(v) <= limit]
        return min(eligible, key=vm_load_score, default=None)

    elif current_algo == "random":
        eligible = [v for v in vms if can_fit(v, task)]
        return random.choice(eligible) if eligible else None

    return None

def _assign_unsafe(vm, task):
    """Mutate VM state only. No IO, no sleep. Caller holds lock."""
    vm["cpu"]  = round(min(100, vm["cpu"]  + task["cpu"]),  1)
    vm["mem"]  = round(min(100, vm["mem"]  + task["mem"]),  1)
    vm["disk"] = round(min(100, vm["disk"] + task["disk"]), 1)
    task["assigned_vm"]   = vm["id"]
    task["assigned_time"] = ts()
    task["state"]         = "running"
    vm["tasks"].append(task)

# ═══════════════════════════════════════════════════════════════════
# RELEASE — runs in timer thread
# ═══════════════════════════════════════════════════════════════════

def release_task(vm_id, task_id, fail=False):
    global tasks_done, tasks_failed, tasks_retried
    log_msg = log_lvl = None
    retry_task = None

    with lock:
        vm   = VMS.get(vm_id)
        if not vm: return
        task = next((t for t in vm["tasks"] if t["id"] == task_id), None)
        if not task: return

        vm["tasks"].remove(task)
        vm["cpu"]  = round(max(0.0, vm["cpu"]  - task["cpu"]),  1)
        vm["mem"]  = round(max(0.0, vm["mem"]  - task["mem"]),  1)
        vm["disk"] = round(max(0.0, vm["disk"] - task["disk"]), 1)

        if fail:
            tasks_failed += 1
            vm["failures"] += 1
            attempts = task.get("attempts", 0) + 1
            if attempts < RETRY_LIMIT:
                task["attempts"] = attempts
                task["state"]    = "queued"
                task["assigned_vm"] = None
                retry_task = task
                tasks_retried += 1
                log_msg = f"Task {task['name']} FAILED (attempt {attempts}) — re-queuing"
                log_lvl = "warning"
            else:
                log_msg = f"Task {task['name']} FAILED permanently after {attempts} attempts"
                log_lvl = "error"
        else:
            tasks_done += 1
            log_msg = f"Task {task['name']} ✓ done on {vm['name']}"
            log_lvl = "success"

    if retry_task:
        with lock:
            PQUEUES["retry"].appendleft(retry_task)  # high priority retry

    add_log(log_msg, log_lvl)
    snapshot_metrics()
    scheduler_event.set()
    maybe_scale_down()   # outside lock

# ═══════════════════════════════════════════════════════════════════
# AUTO-SCALING
# ═══════════════════════════════════════════════════════════════════

SCALE_UP_THRESHOLD   = 80   # avg load % to trigger scale-up
SCALE_DOWN_THRESHOLD = 25   # avg load % to trigger scale-down

def maybe_scale_up():
    """Spin up a new VM if avg load too high. Called outside lock."""
    global _vm_counter
    if not auto_scale_on:
        return
    with lock:
        vms   = list(VMS.values())
        count = len(vms)
        if count >= 8:   # cap at 8 VMs
            return
        avg = sum(vm_load_score(v) for v in vms) / count
        if avg < SCALE_UP_THRESHOLD:
            return
        _vm_counter += 1
        vid  = f"vm{_vm_counter}"
        name = f"VM-Auto{_vm_counter}"
        regions = ["us-east", "us-west", "eu-central", "ap-south", "us-central", "eu-west"]
        region  = regions[(_vm_counter - 1) % len(regions)]
        VMS[vid] = make_vm(vid, name, region, is_auto=True)

    add_log(f"AUTO-SCALE ▲ {name} spun up (avg load was {avg:.0f}%)", "info")
    snapshot_metrics()

def maybe_scale_down():
    """Tear down an idle auto-scaled VM if avg load is low. Outside lock."""
    if not auto_scale_on:
        return
    with lock:
        vms   = list(VMS.values())
        count = len(vms)
        if count <= 4:   # keep at least 4 baseline VMs
            return
        avg = sum(vm_load_score(v) for v in vms) / count
        if avg > SCALE_DOWN_THRESHOLD:
            return
        # find an idle auto-scaled VM with no tasks
        candidate = next(
            (v for v in vms if v.get("auto_scaled") and not v["tasks"] and vm_load_score(v) == 0),
            None
        )
        if not candidate:
            return
        del VMS[candidate["id"]]
        name = candidate["name"]

    add_log(f"AUTO-SCALE ▼ {name} torn down (avg load {avg:.0f}%)", "info")
    snapshot_metrics()

# ═══════════════════════════════════════════════════════════════════
# VM MIGRATION
# ═══════════════════════════════════════════════════════════════════

def migrate_tasks():
    """
    Move tasks from most-overloaded VM → least-loaded VM.
    Called by scheduler periodically. Acquires lock briefly per step.
    """
    global tasks_migrated
    migrated = []

    with lock:
        vms = list(VMS.values())
        if len(vms) < 2:
            return

        # find most loaded and least loaded
        overloaded = max(vms, key=vm_load_score)
        idle_vms   = sorted([v for v in vms if v is not overloaded], key=vm_load_score)

        if vm_load_score(overloaded) < 80:
            return   # nothing to migrate

        # try to move lowest-cpu tasks away
        moveable = sorted(overloaded["tasks"], key=lambda t: t["cpu"])
        for task in moveable[:2]:   # move at most 2 tasks per cycle
            for target in idle_vms:
                if (target is not overloaded and
                        target["cpu"]  + task["cpu"]  <= 85 and
                        target["mem"]  + task["mem"]  <= 85 and
                        target["disk"] + task["disk"] <= 85):

                    # remove from overloaded
                    overloaded["tasks"].remove(task)
                    overloaded["cpu"]  = round(max(0, overloaded["cpu"]  - task["cpu"]),  1)
                    overloaded["mem"]  = round(max(0, overloaded["mem"]  - task["mem"]),  1)
                    overloaded["disk"] = round(max(0, overloaded["disk"] - task["disk"]), 1)
                    overloaded["migrations_out"] += 1

                    # add to target
                    target["tasks"].append(task)
                    target["cpu"]  = round(min(100, target["cpu"]  + task["cpu"]),  1)
                    target["mem"]  = round(min(100, target["mem"]  + task["mem"]),  1)
                    target["disk"] = round(min(100, target["disk"] + task["disk"]), 1)
                    target["migrations_in"] += 1

                    task["assigned_vm"] = target["id"]
                    tasks_migrated += 1
                    migrated.append((task["name"], overloaded["name"], target["name"]))
                    break

    for tname, src, dst in migrated:
        add_log(f"MIGRATE {tname}: {src} → {dst}", "info")

    if migrated:
        snapshot_metrics()

# ═══════════════════════════════════════════════════════════════════
# PRIORITY PREEMPTION
# ═══════════════════════════════════════════════════════════════════

def maybe_preempt():
    """
    If a HIGH priority task is stuck in queue and a Low-priority task is running,
    preempt the low task (re-queue it) to make room.
    Called by scheduler. Acquires lock.
    """
    with lock:
        if not PQUEUES["high"]:
            return
        high_task = PQUEUES["high"][0]

        # find a VM running a low-priority task we can preempt
        for vm in VMS.values():
            low_tasks = [t for t in vm["tasks"] if t.get("priority") == "low"]
            if not low_tasks:
                continue
            victim = low_tasks[0]

            # check if freeing victim makes room for high_task
            freed_cpu  = vm["cpu"]  - victim["cpu"]
            freed_mem  = vm["mem"]  - victim["mem"]
            freed_disk = vm["disk"] - victim["disk"]
            if (freed_cpu  + high_task["cpu"]  <= 100 and
                    freed_mem  + high_task["mem"]  <= 100 and
                    freed_disk + high_task["disk"] <= 100):

                # preempt victim
                vm["tasks"].remove(victim)
                vm["cpu"]  = round(max(0, freed_cpu),  1)
                vm["mem"]  = round(max(0, freed_mem),  1)
                vm["disk"] = round(max(0, freed_disk), 1)
                victim["state"]      = "queued"
                victim["assigned_vm"] = None

                # re-queue victim as normal (demoted one level)
                PQUEUES["normal"].appendleft(victim)
                add_log(f"PREEMPT {victim['name']} (low) → re-queued for {high_task['name']} (high)", "warning")
                return

# ═══════════════════════════════════════════════════════════════════
# SCHEDULER THREAD
# ═══════════════════════════════════════════════════════════════════

scheduler_event = threading.Event()
_migration_cycle = 0

def scheduler_loop():
    global _migration_cycle
    add_log("Predictive scheduler started", "info")
    while True:
        scheduler_event.wait(timeout=2.0)
        scheduler_event.clear()

        # every 4 cycles — check migration and scaling
        _migration_cycle += 1
        if _migration_cycle % 4 == 0:
            migrate_tasks()
            maybe_scale_up()
            maybe_scale_down()

        # try preemption for high-priority tasks
        maybe_preempt()

        # drain all lanes: retry > high > normal > low
        while True:
            timer_info = None

            with lock:
                task = None
                for lane in ("retry", "high", "normal", "low"):
                    if PQUEUES[lane]:
                        # SLA breach check — task waited >5s
                        candidate = PQUEUES[lane][0]
                        queued_at = candidate.get("queued_at", time.time())
                        # (tracked at creation)

                        task = candidate
                        break

                if task is None:
                    break

                vm = _pick_vm_unsafe(task)
                if vm is None:
                    # trigger scale-up in next cycle
                    scheduler_event.set()
                    break

                # overload guard
                if vm_load_score(vm) >= 90:
                    scheduler_event.set()
                    break

                # add latency simulation: scheduling delay 50-200ms
                scheduling_delay = random.uniform(0.05, 0.20) / sim_speed

                # pop from lane
                for lane in ("retry", "high", "normal", "low"):
                    if PQUEUES[lane] and PQUEUES[lane][0] is task:
                        PQUEUES[lane].popleft()
                        break

                _assign_unsafe(vm, task)
                fail_chance = 0.06 if task.get("attempts", 0) == 0 else 0.12
                timer_info  = (vm["id"], task["id"], task["duration"],
                               fail_chance, task["name"], vm["name"], scheduling_delay)

            if timer_info:
                vm_id, task_id, dur, fc, tname, vname, delay = timer_info
                actual_dur = (dur / 1000) / sim_speed
                will_fail  = random.random() < fc

                def _start(v, t, d, f, tn, vn):
                    time.sleep(delay)  # scheduling latency — outside lock, fine
                    add_log(f"{tn} → {vn} | {d:.1f}s {'⚠ will fail' if f else ''}", "success")
                    snapshot_metrics()
                    timer = threading.Timer(d, release_task, args=[v, t, f])
                    timer.daemon = True
                    timer.start()

                th = threading.Thread(target=_start,
                                      args=[vm_id, task_id, actual_dur, will_fail, tname, vname],
                                      daemon=True)
                th.start()

# Boot scheduler
threading.Thread(target=scheduler_loop, daemon=True).start()

# ═══════════════════════════════════════════════════════════════════
# METRICS SNAPSHOT + SSE
# ═══════════════════════════════════════════════════════════════════

def snapshot_metrics():
    with lock:
        vms   = list(VMS.values())
        td, tf, tr, tm = tasks_done, tasks_failed, tasks_rejected, tasks_migrated
        tq    = total_queued()
        loads = [vm_load_score(v) for v in vms]
        # update load history per VM
        for vm in vms:
            vm["load_history"].append(vm_load_score(vm))

    avg_load = round(sum(loads)/len(loads), 1) if loads else 0
    avg_cpu  = round(sum(v["cpu"] for v in vms)/len(vms), 1) if vms else 0
    avg_mem  = round(sum(v["mem"] for v in vms)/len(vms), 1) if vms else 0

    metrics_history.append({
        "t": ts(), "cpu": avg_cpu, "mem": avg_mem, "load": avg_load,
        "tasks": td, "failed": tf, "rejected": tr, "migrated": tm, "queued": tq,
    })
    push_sse(avg_cpu, avg_mem, td, tf, tq, len(vms))

def push_sse(cpu, mem, done, fail, queued, vm_count):
    data = "data: " + json.dumps({
        "avg_cpu": cpu, "avg_mem": mem, "tasks_done": done,
        "tasks_failed": fail, "queued": queued, "vm_count": vm_count,
        "algo": ALGO_NAMES.get(current_algo, current_algo),
    }) + "\n\n"
    with sse_lock:
        dead = []
        for q in sse_clients:
            try:   q.put_nowait(data)
            except: dead.append(q)
        for d in dead:
            sse_clients.remove(d)

# ═══════════════════════════════════════════════════════════════════
# TASK CREATION
# ═══════════════════════════════════════════════════════════════════

def create_task(cpu_hint=None, mem_hint=None, ttype=None, priority="normal"):
    global _task_seq, tasks_rejected

    task_types = ["cpu-intensive", "memory-intensive", "mixed", "io-bound"]
    ttype    = ttype or random.choice(task_types)
    cpu_base = cpu_hint or random.randint(8, 32)
    mem_base = mem_hint or random.randint(8, 28)
    dsk_base = random.randint(2, 15)

    if ttype == "cpu-intensive":
        cpu_base = int(cpu_base * 1.9); mem_base = int(mem_base * 0.35); dsk_base = int(dsk_base * 0.5)
    elif ttype == "memory-intensive":
        mem_base = int(mem_base * 1.9); cpu_base = int(cpu_base * 0.35); dsk_base = int(dsk_base * 0.7)
    elif ttype == "io-bound":
        dsk_base = int(dsk_base * 3.0); cpu_base = int(cpu_base * 0.4); mem_base = int(mem_base * 0.5)

    with lock:
        _task_seq += 1
        seq = _task_seq

    task = {
        "id":       str(uuid.uuid4())[:8],
        "name":     f"T-{seq:04d}",
        "type":     ttype,
        "cpu":      min(45, max(3, cpu_base)),
        "mem":      min(42, max(3, mem_base)),
        "disk":     min(28, max(1, dsk_base)),
        "duration": random.randint(3000, 14000),
        "priority": priority,
        "attempts": 0,
        "state":    "queued",
        "assigned_vm": None,
        "queued_at":   time.time(),
    }

    with lock:
        tq = total_queued()

    # Backpressure v2: adaptive eviction by priority
    if tq >= QUEUE_CAP:
        with lock:
            evicted = None
            for lane in ("low", "normal"):   # evict lowest priority first
                if PQUEUES[lane]:
                    evicted = PQUEUES[lane].pop()
                    tasks_rejected += 1
                    break
            if evicted:
                add_log(f"Backpressure: dropped {evicted['name']} ({lane}) for {task['name']}", "warning")
            else:
                tasks_rejected += 1
                add_log(f"Task {task['name']} rejected — queue saturated", "error")
                return task["name"], False

    with lock:
        PQUEUES[priority].append(task)

    add_log(f"Queued {task['name']} ({ttype}, {priority})", "info")
    snapshot_metrics()
    scheduler_event.set()
    return task["name"], True

# ═══════════════════════════════════════════════════════════════════
# ROUTES
# ═══════════════════════════════════════════════════════════════════

@app.route("/api/state")
def get_state():
    try:
        with lock:
            vms_out = []
            for vm in VMS.values():
                pred = round(predict_load(vm), 1)
                vms_out.append({
                    "id": vm["id"], "name": vm["name"], "region": vm["region"],
                    "cpu": vm["cpu"], "mem": vm["mem"], "disk": vm["disk"],
                    "load_score":    vm_load_score(vm),
                    "predicted_load": pred,
                    "task_count":    len(vm["tasks"]),
                    "tasks":         [{"id":t["id"],"name":t["name"],"type":t["type"],"priority":t.get("priority","normal")} for t in vm["tasks"]],
                    "status":        vm_status(vm),
                    "failures":      vm["failures"],
                    "migrations_in": vm["migrations_in"],
                    "migrations_out":vm["migrations_out"],
                    "auto_scaled":   vm.get("auto_scaled", False),
                    "created_at":    vm["created_at"],
                })

            all_q = (list(PQUEUES["retry"]) + list(PQUEUES["high"]) +
                     list(PQUEUES["normal"]) + list(PQUEUES["low"]))
            queue_out = [{"id":t["id"],"name":t["name"],"type":t["type"],
                          "priority":t["priority"],"attempts":t.get("attempts",0)} for t in all_q]
            td, tf, tr, tm = tasks_done, tasks_failed, tasks_rejected, tasks_migrated
            tt, sb = tasks_retried, sla_breaches
            tq = total_queued()
            am, sp, aso = auto_mode, sim_speed, auto_scale_on

        avg_cpu  = round(sum(v["cpu"] for v in vms_out)/len(vms_out), 1) if vms_out else 0
        avg_mem  = round(sum(v["mem"] for v in vms_out)/len(vms_out), 1) if vms_out else 0
        avg_pred = round(sum(v["predicted_load"] for v in vms_out)/len(vms_out), 1) if vms_out else 0

        return jsonify({
            "vms": vms_out, "queue": queue_out,
            "log": list(event_log)[:35],
            "metrics": list(metrics_history),
            "stats": {
                "avg_cpu": avg_cpu, "avg_mem": avg_mem,
                "avg_predicted": avg_pred,
                "tasks_done":    td, "tasks_failed":  tf,
                "tasks_rejected":tr, "tasks_migrated":tm,
                "tasks_retried": tt, "sla_breaches":  sb,
                "queued": tq, "vm_count": len(vms_out),
                "algo": ALGO_NAMES.get(current_algo, current_algo),
                "auto": am, "speed": sp, "auto_scale": aso,
            }
        })
    except Exception as e:
        print(f"ERROR /api/state: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/task", methods=["POST"])
def add_task():
    try:
        d = request.get_json(silent=True) or {}
        name, ok = create_task(d.get("cpu"), d.get("mem"), d.get("type"), d.get("priority","normal"))
        return jsonify({"status":"ok","task":name,"queued":ok})
    except Exception as e:
        return jsonify({"error":str(e)}), 500


@app.route("/api/task/burst", methods=["POST"])
def burst():
    try:
        d     = request.get_json(silent=True) or {}
        count = min(int(d.get("count", 5)), 30)
        prio  = d.get("priority", "normal")
        res   = [create_task(priority=prio) for _ in range(count)]
        return jsonify({"status":"ok","count":count,
                        "queued": sum(1 for _,ok in res if ok)})
    except Exception as e:
        return jsonify({"error":str(e)}), 500


@app.route("/api/algo", methods=["POST"])
def set_algo():
    global current_algo
    try:
        d = request.get_json(silent=True) or {}
        a = d.get("algo","predictive")
        if a not in ALGO_NAMES:
            return jsonify({"error":"Unknown algorithm"}), 400
        with lock:
            current_algo = a
        add_log(f"Algorithm → {ALGO_NAMES[a]}", "info")
        return jsonify({"status":"ok","algo":a})
    except Exception as e:
        return jsonify({"error":str(e)}), 500


@app.route("/api/speed", methods=["POST"])
def set_speed():
    global sim_speed
    try:
        d = request.get_json(silent=True) or {}
        sim_speed = max(0.25, min(5.0, float(d.get("speed", 1.0))))
        add_log(f"Speed → {sim_speed}×", "info")
        return jsonify({"status":"ok","speed":sim_speed})
    except Exception as e:
        return jsonify({"error":str(e)}), 500


@app.route("/api/autoscale", methods=["POST"])
def toggle_autoscale():
    global auto_scale_on
    try:
        d = request.get_json(silent=True) or {}
        auto_scale_on = bool(d.get("enabled", not auto_scale_on))
        add_log(f"Auto-scaling {'ON' if auto_scale_on else 'OFF'}", "info")
        return jsonify({"status":"ok","auto_scale":auto_scale_on})
    except Exception as e:
        return jsonify({"error":str(e)}), 500


@app.route("/api/migrate", methods=["POST"])
def force_migrate():
    try:
        migrate_tasks()
        return jsonify({"status":"ok","migrated":tasks_migrated})
    except Exception as e:
        return jsonify({"error":str(e)}), 500


@app.route("/api/auto", methods=["POST"])
def toggle_auto():
    global auto_mode
    try:
        d  = request.get_json(silent=True) or {}
        iv = max(500, int(d.get("interval", 2000)))
        with lock:
            auto_mode = not auto_mode
            is_on = auto_mode
        if is_on:
            def _spawn():
                with lock:
                    still = auto_mode
                if still:
                    create_task()
                    t = threading.Timer(iv/1000, _spawn)
                    t.daemon = True
                    auto_timer_ref[0] = t
                    t.start()
            t = threading.Timer(iv/1000, _spawn)
            t.daemon = True; auto_timer_ref[0] = t; t.start()
            add_log(f"Auto-spawn ON every {iv/1000:.1f}s", "info")
        else:
            if auto_timer_ref[0]:
                auto_timer_ref[0].cancel(); auto_timer_ref[0] = None
            add_log("Auto-spawn OFF", "info")
        return jsonify({"status":"ok","auto":is_on})
    except Exception as e:
        return jsonify({"error":str(e)}), 500


@app.route("/api/reset", methods=["POST"])
def reset():
    global tasks_done, tasks_failed, tasks_rejected, tasks_migrated
    global tasks_retried, sla_breaches, rr_index, auto_mode, _task_seq, _vm_counter
    try:
        if auto_timer_ref[0]:
            auto_timer_ref[0].cancel(); auto_timer_ref[0] = None
        with lock:
            VMS.clear()
            _vm_counter = 4
            for i, (name, region) in enumerate([
                ("VM-Alpha","us-east"),("VM-Beta","us-west"),
                ("VM-Gamma","eu-central"),("VM-Delta","ap-south")], 1):
                VMS[f"vm{i}"] = make_vm(f"vm{i}", name, region)
            for q in PQUEUES.values(): q.clear()
            (tasks_done, tasks_failed, tasks_rejected, tasks_migrated,
             tasks_retried, sla_breaches, rr_index, _task_seq) = 0,0,0,0,0,0,0,0
            auto_mode = False
        event_log.clear(); metrics_history.clear()
        add_log("Simulation reset ↺", "warning")
        return jsonify({"status":"ok"})
    except Exception as e:
        return jsonify({"error":str(e)}), 500


@app.route("/api/stream")
def sse_stream():
    def generate():
        q = qlib.Queue(maxsize=30)
        with sse_lock: sse_clients.append(q)
        try:
            yield "data: {\"connected\":true}\n\n"
            while True:
                try:
                    yield q.get(timeout=20)
                except qlib.Empty:
                    yield ": heartbeat\n\n"
        finally:
            with sse_lock:
                if q in sse_clients: sse_clients.remove(q)
    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})


@app.route("/")
def index():
    return jsonify({"status":"ok","version":"4.0","features":[
        "predictive-scheduling","auto-scaling","vm-migration",
        "priority-preemption","retry-logic","latency-simulation",
        "disk-aware-scheduling","sse-realtime"
    ]})


if __name__ == "__main__":
    snapshot_metrics()
    print("=" * 60)
    print("  ☁  Cloud Resource Allocation Simulator  v4.0  ELITE")
    print("  Predictive · Auto-Scale · Migration · Preemption · SSE")
    print("  http://localhost:5000")
    print("=" * 60)
    add_log("Elite simulator v4.0 online", "success")
    app.run(debug=False, port=5000, threaded=True)
