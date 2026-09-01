bl_info = {
    "name": "Find Similar Parts",
    "author": "-",
    "version": (13, 0, 0),
    "blender": (3, 0, 0),
    "location": "3D View > Edit Mode > N-панель > Similar",
    "description": "Поиск конгруэнтных фрагментов меша по выделению + превращение их в инстансы",
    "category": "Mesh",
}

import bpy
import bmesh
import numpy as np
from collections import defaultdict
from mathutils import Matrix, Vector
from mathutils.kdtree import KDTree
from mathutils.bvhtree import BVHTree

BOUNDARY = 9.0
STEP_BUDGET = 4000


# ---------------------------------------------------------------- geometry

def face_verts(f):
    return [l.vert for l in f.loops]


def ordered_from(f, a, b):
    vs = face_verts(f)
    if a not in vs:
        return None
    i = vs.index(a)
    vs = vs[i:] + vs[:i]
    if vs[1] is b:
        return vs
    vs = [vs[0]] + list(reversed(vs[1:]))
    return vs if vs[1] is b else None


def cyc_lengths(vs):
    n = len(vs)
    return [(vs[i].co - vs[(i + 1) % n].co).length for i in range(n)]


def dihedral(e):
    if len(e.link_faces) == 2:
        try:
            return e.calc_face_angle_signed(0.0)
        except ValueError:
            return BOUNDARY
    return BOUNDARY


def build_cache(bm):
    cache = {}
    for f in bm.faces:
        el = sorted(e.calc_length() for e in f.edges)
        cache[f.index] = (len(f.verts), f.calc_area(), el, sum(el))
    return cache


def faces_match(i, j, cache, ltol):
    a, b = cache[i], cache[j]
    if a[0] != b[0]:
        return False
    if abs(a[1] - b[1]) > ltol * max(a[3], 1e-6) * 2.0:
        return False
    for x, y in zip(a[2], b[2]):
        if abs(x - y) > ltol:
            return False
    return True


def components(faces):
    pool = set(faces)
    out = []
    while pool:
        comp = {pool.pop()}
        stack = list(comp)
        while stack:
            f = stack.pop()
            for e in f.edges:
                for g in e.link_faces:
                    if g in pool:
                        pool.discard(g)
                        comp.add(g)
                        stack.append(g)
        out.append(comp)
    return out


def loose_parts(bm, cache):
    pid, stats, cur = {}, {}, 0
    for f in bm.faces:
        if f.index in pid:
            continue
        pid[f.index] = cur
        stack = [f]
        n, ar = 0, 0.0
        while stack:
            g = stack.pop()
            n += 1
            ar += cache[g.index][1]
            for e in g.edges:
                for h in e.link_faces:
                    if h.index not in pid:
                        pid[h.index] = cur
                        stack.append(h)
        stats[cur] = (n, ar)
        cur += 1
    return pid, stats


def rigid_fit(pairs):
    """Kabsch. y = x @ R.T + t"""
    P = np.array([list(a) for a, _ in pairs], dtype=np.float64)
    Q = np.array([list(b) for _, b in pairs], dtype=np.float64)
    cp, cq = P.mean(0), Q.mean(0)
    H = (P - cp).T @ (Q - cq)
    U, S, Vt = np.linalg.svd(H)
    R = Vt.T @ U.T
    det = np.linalg.det(R)
    if det < 0 and (S[0] < 1e-12 or S[-1] < 1e-6 * S[0]):
        R = Vt.T @ np.diag([1.0, 1.0, -1.0]) @ U.T
        det = np.linalg.det(R)
    t = cq - cp @ R.T
    dev = float(np.abs(P @ R.T + t - Q).max()) if len(P) else 0.0
    return R, t, dev, det < 0


def orient_ok(R, orientation, s_ang):
    if orientation == 'SAME':
        return np.abs(R - np.eye(3)).max() <= s_ang
    if orientation == 'Z':
        ez = np.array([0.0, 0.0, 1.0])
        return np.abs(R @ ez - ez).max() <= s_ang
    return True


def coord_scale(verts):
    m = 0.0
    for v in verts:
        m = max(m, abs(v.co.x), abs(v.co.y), abs(v.co.z))
    return m


def f32_noise(m):
    """Шум округления float32 на таких координатах (4 ULP)."""
    return float(np.spacing(np.float32(max(m, 1.0)))) * 4.0


def patch_diag(verts):
    co = np.array([list(v.co) for v in verts], dtype=np.float64)
    return float(np.linalg.norm(co.max(0) - co.min(0))) if len(co) else 0.0


def resolve_eps(sverts, opts):
    """Порог посадки: ручной, либо max(допуск x4, шум float32, % диагонали)."""
    m = coord_scale(sverts)
    noise = f32_noise(m)
    if opts["eps_override"] > 0:
        return opts["eps_override"], noise, m
    rel = patch_diag(sverts) * opts["rel_tol"] / 100.0
    return max(opts["tol"] * 4.0, noise, rel, 1e-6), noise, m


# ---------------------------------------------------------------- проверка по поверхности

def selection_isolated(sel):
    """True, если у граней выделения нет ни одного соседа вне выделения,
    то есть выделены целиком отдельные объекты (один или несколько луз партсов).
    Для таких дозаливка безопасна: расти она может только по смежным рёбрам,
    а снаружи отдельного меша смежных граней нет."""
    ss = set(sel)
    for f in sel:
        for e in f.edges:
            for g in e.link_faces:
                if g not in ss:
                    return False
    return True


def resolve_flood(mode, sel):
    if mode == 'ON':
        return True, "вкл"
    if mode == 'OFF':
        return False, "выкл"
    iso = selection_isolated(sel)
    return iso, ("авто:вкл, объект изолирован" if iso
                 else "авто:выкл, есть соседняя геометрия")


def make_patch_bvh(sel):
    idx, co, poly = {}, [], []
    for f in sel:
        ids = []
        for v in f.verts:
            if v not in idx:
                idx[v] = len(co)
                co.append(v.co.copy())
            ids.append(idx[v])
        poly.append(ids)
    return BVHTree.FromPolygons(co, poly, all_triangles=False)


def face_inside_patch(h, R, t, eps, bvh_patch):
    """Грань считается лежащей на исходном фрагменте, только если ВСЕ её вершины
    и центр ложатся на него. Одного центра мало: у тонкого полигона центр почти
    совпадает с ребром и попадает на фрагмент, хотя сама грань снаружи."""
    pts = [h.calc_center_median()] + [v.co for v in h.verts]
    for p in pts:
        q = (np.array(list(p)) - t) @ R
        loc, _, _, d = bvh_patch.find_nearest(Vector(q), eps * 4.0)
        if loc is None or d > eps:
            return False
    return True


def surface_collect(bm, bvh_mesh, bvh_patch, probe, R, t, eps, cos_a,
                    need, require_normal, flood, seeds=None, cap=100000):
    """Переносим фрагмент преобразованием R,t и собираем грани цели по поверхности.
    Возвращает (набор граней, сколько добавила дозаливка)."""
    hits = set(seeds) if seeds else set()
    miss = 0
    for f in probe:
        p = np.array(list(f.calc_center_median())) @ R.T + t
        loc, nrm, i, d = bvh_mesh.find_nearest(Vector(p), eps * 4.0)
        ok = loc is not None and d <= eps
        if ok and require_normal:
            n_ref = R @ np.array(list(f.normal))
            if abs(float(np.dot(n_ref, np.array(list(nrm))))) < cos_a:
                ok = False
        if ok:
            hits.add(bm.faces[i])
        else:
            miss += 1
            if len(probe) - miss < need:
                return None
    if not hits:
        return None
    if not flood:
        return hits, 0

    faces = set(hits)
    frontier = list(hits)
    while frontier:
        g = frontier.pop()
        for e in g.edges:
            for h in e.link_faces:
                if h in faces:
                    continue
                if face_inside_patch(h, R, t, eps, bvh_patch):
                    faces.add(h)
                    frontier.append(h)
                    if len(faces) > cap:
                        return faces, len(faces) - len(hits)
    return faces, len(faces) - len(hits)


def uv_match(fmap, vmap, uvl, mode, uv_tol):
    """Сверка развёртки для топологически сопоставленных граней.
    SAME  — UV должны совпадать абсолютно (запечённые инстансы в общем UV).
    SHIFT — совпадать с точностью до одного общего сдвига (остров UV сдвинут)."""
    off = None
    for f, g in fmap.items():
        gl = {l.vert: l for l in g.loops}
        for l in f.loops:
            tv = vmap.get(l.vert)
            if tv is None:
                return False
            l2 = gl.get(tv)
            if l2 is None:
                return False
            d = l2[uvl].uv - l[uvl].uv
            if mode == 'SAME':
                if d.length > uv_tol:
                    return False
            else:
                if off is None:
                    off = d
                elif (d - off).length > uv_tol:
                    return False
    return True


# ---------------------------------------------------------------- matcher (по граням)

def bfs_order(anchor, comp):
    seen = {anchor}
    queue = [anchor]
    order = []
    qi = 0
    while qi < len(queue):
        f = queue[qi]
        qi += 1
        for e in f.edges:
            for g in e.link_faces:
                if g in comp and g not in seen:
                    seen.add(g)
                    order.append((g, e))
                    queue.append(g)
    return order


def frame(p0, p1, p2):
    """Ортонормированный репер треугольника."""
    u = p1 - p0
    nu = np.linalg.norm(u)
    if nu < 1e-12:
        return None
    u = u / nu
    w = p2 - p0
    w = w - u * float(np.dot(w, u))
    nw = np.linalg.norm(w)
    if nw < 1e-12:
        return None
    w = w / nw
    return np.column_stack((u, w, np.cross(u, w)))


def fit_stats(pts):
    """Центроид и второе сингулярное число облака исходных точек.
    Второе число — «ширина» опоры: у тонкого треугольника оно близко к нулю,
    и предсказание положения дальних вершин по нему недостоверно."""
    P = np.array([list(x) for x in pts], dtype=np.float64)
    c = P.mean(0)
    sv = np.linalg.svd(P - c, compute_uv=False)
    return c, (float(sv[1]) if len(sv) > 1 else 0.0)


def tri_transform(P, Q):
    """Преобразование по трём точкам через реперы — без SVD.
    На якоре вызывается сотни тысяч раз, Кабш там неоправданно дорог."""
    Bp, Bq = frame(*P), frame(*Q)
    if Bp is None or Bq is None:
        return None
    R = Bq @ Bp.T
    return R, Q[0] - P[0] @ R.T


def try_match(anchor, cand, start, rev, comp, order, cache, ltol, atol,
              strict, need, eps, stats):
    """Обход с бэктрекингом, но ведомый геометрией: на каждом шаге кандидаты
    отбираются и сортируются по отклонению от текущего преобразования.
    Без этого при большом допуске ветвление взрывается и обход выедает бюджет."""
    av = face_verts(anchor)
    cv = face_verts(cand)
    k = len(av)
    if len(cv) != k:
        return None
    seq = cv[start:] + cv[:start]
    if rev:
        seq = [seq[0]] + list(reversed(seq[1:]))
    for x, y in zip(cyc_lengths(av), cyc_lengths(seq)):
        if abs(x - y) > ltol:
            return None

    vmap, used = {}, {}
    for a, b in zip(av, seq):
        prev = vmap.get(a)
        if prev is not None:
            if prev is not b:
                return None
        elif used.get(b) not in (None, a):
            return None
        else:
            vmap[a] = b
            used[b] = a

    P = [np.array(list(v.co)) for v in av]
    Q = [np.array(list(v.co)) for v in seq]
    if k == 3:
        tr = tri_transform(P, Q)
        if tr is None:
            return None
        R0, t0 = tr
    else:
        R0, t0, dv, _ = rigid_fit(list(zip([v.co for v in av],
                                           [v.co for v in seq])))
        if dv > eps:
            return None

    cen0, s1_0 = fit_stats([v.co for v in av])
    # 0..7 как раньше, 8 — центроид опоры, 9 — её «ширина» s1
    state = [vmap, used, {anchor: cand}, {cand}, 0, R0, t0, 0, cen0, s1_0]

    def snapshot():
        return [dict(state[0]), dict(state[1]), dict(state[2]), set(state[3]),
                state[4], state[5], state[6], state[7], state[8], state[9]]

    def restore(s):
        state[0] = dict(s[0]); state[1] = dict(s[1]); state[2] = dict(s[2])
        state[3] = set(s[3]); state[4] = s[4]
        state[5] = s[5]; state[6] = s[6]; state[7] = s[7]
        state[8] = s[8]; state[9] = s[9]

    def pred_limit(co):
        """Допуск на предсказание для конкретной вершины.
        Угловая неопределённость преобразования ~ eps / ширина опоры, на
        расстоянии D от центра опоры она даёт снос D * eps / s1. Пока опора
        узкая, допуск огромен и фильтр просто не мешает обходу."""
        s1 = state[9]
        if s1 <= 1e-9:
            return float('inf')
        d = float(np.linalg.norm(np.array(list(co)) - state[8]))
        return eps * (1.0 + d / s1)

    def options(i):
        f2, e = order[i]
        a, b = e.verts[0], e.verts[1]
        A, B = state[0].get(a), state[0].get(b)
        if A is None or B is None:
            return []
        ge = next((x for x in A.link_edges if x.other_vert(A) is B), None)
        if ge is None:
            return []
        if strict and len(e.link_faces) == 2 and len(ge.link_faces) == 2:
            if abs(abs(dihedral(e)) - abs(dihedral(ge))) > atol:
                return []
        sf = ordered_from(f2, a, b)
        if sf is None:
            return []
        R, t = state[5], state[6]
        pred = [(np.array(list(v.co)) @ R.T + t, pred_limit(v.co)) for v in sf]
        out = []
        for g2 in ge.link_faces:
            if g2 in state[3]:
                continue
            if not faces_match(f2.index, g2.index, cache, ltol):
                continue
            sg = ordered_from(g2, A, B)
            if sg is None or len(sg) != len(sf):
                continue
            score, bad = 0.0, False
            for (pc, lim), y in zip(pred, sg):
                dd = float(np.abs(pc - np.array(list(y.co))).max())
                if dd > lim:
                    bad = True
                    break
                rel = dd / lim if lim > 0 else 0.0
                if rel > score:
                    score = rel
            if not bad:
                out.append((score, g2, sg))
        out.sort(key=lambda z: z[0])
        return [(z[1], z[2]) for z in out]

    def apply(i, item):
        f2, _ = order[i]
        g2, sg = item
        sf = ordered_from(f2, order[i][1].verts[0], order[i][1].verts[1])
        vm, us = state[0], state[1]
        for x, y in zip(sf, sg):
            prev = vm.get(x)
            if prev is not None:
                if prev is not y:
                    return False
            elif us.get(y) not in (None, x):
                return False
            else:
                vm[x] = y
                us[y] = x
        state[2][f2] = g2
        state[3].add(g2)
        if len(state[2]) > stats["depth"]:
            stats["depth"] = len(state[2])
        state[7] += 1
        # пока опора узкая, пересчитываем на каждом шаге: она быстро расширяется
        # и предсказание из бесполезного становится точным
        narrow = state[9] < eps * 8.0
        if narrow or state[7] >= 16:
            state[7] = 0
            pairs = [(x.co, y.co) for x, y in vm.items()]
            R2, t2, dv, _ = rigid_fit(pairs)
            if dv > eps:
                return False
            state[5], state[6] = R2, t2
            state[8], state[9] = fit_stats([x for x, _ in pairs])
        return True

    n = len(order)
    total = len(comp)
    pending, snaps = {}, {}
    i, steps = 0, 0

    while True:
        if i == n:
            if len(state[2]) >= need:
                return dict(state[2]), dict(state[0])
            adv = False
        else:
            if i not in pending:
                pending[i] = options(i) + [None]
                snaps[i] = snapshot()
            adv = False
            while pending[i]:
                item = pending[i].pop(0)
                restore(snaps[i])
                steps += 1
                if steps > STEP_BUDGET:
                    stats["budget"] += 1
                    return None
                if item is None:
                    if total - (state[4] + 1) >= need:
                        state[4] += 1
                        adv = True
                    break
                if apply(i, item):
                    adv = True
                    break
        if adv:
            i += 1
        else:
            pending.pop(i, None)
            snaps.pop(i, None)
            i -= 1
            while i >= 0 and not pending.get(i):
                pending.pop(i, None)
                snaps.pop(i, None)
                i -= 1
            if i < 0:
                return None


def face_width(f):
    """Наименьшая высота грани: мера «нетонкости». У слайвера близка к нулю."""
    el = max((e.calc_length() for e in f.edges), default=0.0)
    return (2.0 * f.calc_area() / el) if el > 1e-12 else 0.0


def pick_anchors(comp, rar, n, min_width):
    """Редкие, разнесённые и НЕвырожденные грани. Тонкий треугольник — худший
    возможный якорь: его репер плохо обусловлен, и предсказание положения
    соседей по нему уезжает тем сильнее, чем дальше вершина от якоря.
    При этом по редкости слайверы всплывают наверх, так что их надо отсекать
    явно, иначе обход систематически стартует с самого неудачного места."""
    good = [f for f in comp if face_width(f) >= min_width]
    if len(good) < max(n, 3):
        good = sorted(comp, key=lambda f: -face_width(f))[:max(n * 5, 8)]
    pool = sorted(good, key=rar)[:max(n * 5, int(len(good) * 0.3) + 1)]
    if not pool:
        return []
    out = [pool[0]]
    cen = {f: f.calc_center_median() for f in pool}
    while len(out) < min(n, len(pool)):
        best, bestd = None, -1.0
        for f in pool:
            if f in out:
                continue
            dmin = min((cen[f] - cen[g]).length for g in out)
            if dmin > bestd:
                best, bestd = f, dmin
        if best is None:
            break
        out.append(best)
    return out


def search_faces(bm, opts):
    sel = [f for f in bm.faces if f.select]
    if not sel:
        return [], [], ""

    strict = (opts["method"] == 'STRICT')
    atol = np.radians(max(opts["angle_tol"], 0.0))
    cos_a = float(np.cos(atol))
    sel_set = set(sel)
    cache = build_cache(bm)

    sverts = {v for f in sel for v in f.verts}
    eps, noise, mscale = resolve_eps(sverts, opts)
    ltol = max(opts["tol"], noise)

    comps = components(sel)
    comps.sort(key=len, reverse=True)
    main = comps[0]
    probe = list(main) if opts["ignore_extra"] else sel

    pid, pstats = loose_parts(bm, cache)
    pcount = defaultdict(int)
    for f in bm.faces:
        pcount[pid[f.index]] += 1
    selcount = defaultdict(int)
    for f in sel:
        selcount[pid[f.index]] += 1
    allowed_pids = None
    if (opts["use_parts"] and len(pstats) > 1
            and all(pcount[p] == c for p, c in selcount.items())):
        # выделены только целые отдельные объекты — сузим кандидатов до объектов
        # с тем же числом граней и площадью, что у части с главной компонентой
        p0 = pid[next(iter(main)).index]
        n0, a0 = pstats[p0]
        allowed_pids = {p for p, (n, a) in pstats.items()
                        if n == n0 and abs(a - a0) <= max(ltol * 20.0 * a0, 1e-9)}
        if len(allowed_pids) < 2:
            allowed_pids = None  # кандидатов нет — фильтр только вредит

    # Сетка для ранжирования якорей НЕ должна грубеть вместе с допуском:
    # при допуске 5 см бины по 50 см схлопывали все грани в один, и выбор
    # самой редкой грани превращался в выбор случайной.
    q = max(min(ltol, 0.02), noise * 4.0, 1e-4)
    hist, keys = defaultdict(int), {}
    for f in bm.faces:
        n, ar, el, per = cache[f.index]
        k = (n, int(ar / (q * q)), tuple(int(x / q) for x in el))
        keys[f.index] = k
        hist[k] += 1

    anchors = pick_anchors(main, lambda f: hist[keys[f.index]],
                           opts["anchor_tries"], eps * 8.0)
    need_topo = max(1, int(np.ceil(opts["min_match"] * len(main))))
    need_surf = max(1, int(np.ceil(opts["min_match"] * len(probe))))
    s_ang = max(np.sin(atol), 1e-6)

    bm.faces.ensure_lookup_table()
    bvh_mesh = BVHTree.FromBMesh(bm)
    bvh_patch = make_patch_bvh(sel)

    use_flood, flood_note = resolve_flood(opts["flood"], sel)
    uvl = bm.loops.layers.uv.active
    uv_on = opts["uv_mode"] != 'ANY' and uvl is not None
    uv_note = ""
    if opts["uv_mode"] != 'ANY' and uvl is None:
        uv_note = ", UV-слоя нет — сверка UV пропущена"

    claimed, results = set(sel_set), []
    d = defaultdict(int)

    # списки кандидатов считаем заранее и начинаем с самого избирательного
    # якоря: он заявит совпадения первым, остальным останется меньше работы
    plans = []
    for anchor in anchors:
        cf = [f for f in bm.faces
              if (allowed_pids is None or pid[f.index] in allowed_pids)
              and faces_match(anchor.index, f.index, cache, ltol)]
        plans.append((len(cf), anchor, cf))
        d["cand"] += len(cf)
    plans.sort(key=lambda z: z[0])
    d["cand_best"] = plans[0][0] if plans else 0

    for _, anchor, cand_faces in plans:
        if len(results) >= opts["max_results"]:
            break
        order = bfs_order(anchor, main)
        k = len(anchor.verts)

        for cand in cand_faces:
            if cand in claimed:
                continue
            found = False
            for rev in (False, True):
                if found:
                    break
                for start in range(k):
                    m = try_match(anchor, cand, start, rev, main, order,
                                  cache, ltol, atol, strict, need_topo, eps, d)
                    if m is None:
                        continue
                    d["topo"] += 1
                    fmap, vmap = m
                    R, t, dev, mirror = rigid_fit(
                        [(a.co, b.co) for a, b in vmap.items()])
                    if dev > eps:
                        d["fit"] += 1
                        continue
                    if mirror and not opts["allow_mirror"]:
                        d["mirror"] += 1
                        continue
                    if not orient_ok(R, opts["orientation"], s_ang):
                        d["orient"] += 1
                        continue
                    if uv_on and not uv_match(fmap, vmap, uvl,
                                              opts["uv_mode"], opts["uv_tol"]):
                        d["uv"] += 1
                        continue

                    got = surface_collect(
                        bm, bvh_mesh, bvh_patch, probe, R, t, eps, cos_a,
                        need_surf, opts["require_normal"], use_flood,
                        seeds=set(fmap.values()))
                    if got is None:
                        d["surf"] += 1
                        continue
                    faces, nfl = got
                    d["flood"] += nfl
                    if faces & claimed:
                        d["overlap"] += 1
                        continue
                    claimed |= faces
                    results.append((faces, R, t, mirror))
                    found = True
                    break
            if len(results) >= opts["max_results"]:
                break

    hint = ""
    deep = d["depth"]
    if d["topo"] == 0 and deep and deep < len(main) * 0.25:
        hint = (" | ОБХОД ГЛОХНЕТ СРАЗУ — топология копий отличается, "
                "пробуйте метод «по плоским областям»")
    elif d["topo"] == 0 and deep >= len(main) * 0.75:
        hint = (f" | ОБХОД ДОХОДИТ ДО {deep}/{len(main)} — снизьте долю "
                f"совпадения до {max(0.3, (deep - 1) / len(main)):.2f}")
    elif d["budget"] > d["topo"] and d["budget"] > 0:
        hint = " | ОБХОД СДАЛСЯ ПО БЮДЖЕТУ — снизьте допуск"
    elif d["cand_best"] > 20000:
        hint = " | сигнатура не различает грани — снизьте допуск"
    info = (f"кандид. {d['cand']} (лучший якорь {d['cand_best']}), "
            f"бюджет {d['budget']}, лучший обход {d['depth']}/{len(main)} "
            f"/ топол. {d['topo']} / отсев: "
            f"посадка {d['fit']}, поверхн. {d['surf']}, ориент. {d['orient']}, "
            f"зеркало {d['mirror']}, UV {d['uv']}, пересеч. {d['overlap']} | "
            f"дозал. {flood_note} +{d['flood']} гр., топол. {need_topo}/{len(main)}, поверхн. {need_surf}/{len(probe)}, "
            f"eps {eps:.4f} (float32 {noise:.4f} при |xyz| {mscale:.0f})"
            f"{uv_note}{hint}")
    return sel, results, info


# ---------------------------------------------------------------- matcher (по областям)

def build_regions(bm, ptol):
    rid, regions = {}, []
    for f in bm.faces:
        if f.index in rid:
            continue
        r = len(regions)
        rid[f.index] = r
        comp, stack = [f], [f]
        while stack:
            g = stack.pop()
            for e in g.edges:
                if len(e.link_faces) != 2:
                    continue
                a = abs(dihedral(e))
                if a >= BOUNDARY or a > ptol:
                    continue
                for h in e.link_faces:
                    if h.index not in rid:
                        rid[h.index] = r
                        comp.append(h)
                        stack.append(h)
        regions.append(comp)
    return rid, regions


def region_normal(comp):
    n = Vector((0.0, 0.0, 0.0))
    for f in comp:
        n += f.normal * f.calc_area()
    return n.normalized() if n.length > 1e-12 else Vector((0.0, 0.0, 1.0))


def feat_eq(a, b, atol):
    if a[0] != b[0]:
        return False
    for x, y in zip(a[1], b[1]):
        if abs(x - y) > atol:
            return False
    return True


def make_bases(pverts, rar, span, n):
    """Несколько базовых троек вместо одной (идея RANSAC-перезапусков из 4PCS).
    Одна тройка — одна точка отказа: если у её первой вершины не нашлось
    соответствия по признаку, весь режим возвращает ноль при исправной модели."""
    ranked = sorted(pverts, key=rar)
    bases, seen = [], set()
    for p1 in ranked[:max(n * 3, 6)]:
        far = [v for v in pverts if v is not p1 and (v.co - p1.co).length > span]
        if not far:
            far = [v for v in pverts if v is not p1]
        far.sort(key=lambda v: (rar(v), -(v.co - p1.co).length))
        for p2 in far[:3]:
            e = p2.co - p1.co
            d12 = e.length
            if d12 < 1e-9:
                continue
            en = e / d12
            for p3 in ranked:
                if p3 is p1 or p3 is p2:
                    continue
                dv = p3.co - p1.co
                if dv.length < span * 0.3:
                    continue
                if dv.normalized().cross(en).length < 0.1:
                    continue
                key = tuple(sorted((p1.index, p2.index, p3.index)))
                if key in seen:
                    break
                seen.add(key)
                bases.append((p1, p2, p3))
                break
            if len(bases) >= n:
                return bases
    return bases


def search_regions(bm, opts):
    sel = [f for f in bm.faces if f.select]
    if not sel:
        return [], [], ""

    atol = np.radians(max(opts["angle_tol"], 0.0))
    cos_a = float(np.cos(atol))
    ptol = np.radians(max(opts["planar_tol"], 0.0))
    sel_set = set(sel)

    sverts = {v for f in sel for v in f.verts}
    eps, noise, mscale = resolve_eps(sverts, opts)
    ltol = max(opts["tol"], noise)

    rid, regions = build_regions(bm, ptol)
    rnorm = [region_normal(c) for c in regions]

    vregs = defaultdict(set)
    for i, comp in enumerate(regions):
        for f in comp:
            for v in f.verts:
                vregs[v].add(i)

    def vfeat(v):
        ns = [rnorm[i] for i in sorted(vregs[v])]
        ds = []
        for i in range(len(ns)):
            for j in range(i + 1, len(ns)):
                dd = max(-1.0, min(1.0, ns[i].dot(ns[j])))
                ds.append(float(np.arccos(dd)))
        return (len(ns), tuple(sorted(ds)))

    feats = {v: vfeat(v) for v in bm.verts}
    aq = max(atol, 0.02)
    hist = defaultdict(int)
    for v in bm.verts:
        hist[(feats[v][0], tuple(int(x / aq) for x in feats[v][1]))] += 1

    def rar(v):
        return hist[(feats[v][0], tuple(int(x / aq) for x in feats[v][1]))]

    pverts = list(sverts)
    if len(pverts) < 3:
        return sel, [], "меньше трёх вершин"

    span = max(patch_diag(pverts) * 0.2, ltol * 10.0)
    bases = make_bases(pverts, rar, span, opts["base_tries"])
    if not bases:
        return sel, [], "не удалось построить ни одной невырожденной базовой тройки"

    kd = KDTree(len(bm.verts))
    for v in bm.verts:
        kd.insert(v.co, v.index)
    kd.balance()

    bm.faces.ensure_lookup_table()
    bvh_mesh = BVHTree.FromBMesh(bm)
    bvh_patch = make_patch_bvh(sel)

    s_ang = max(np.sin(atol), 1e-6)
    need_surf = max(1, int(np.ceil(opts["min_match"] * len(sel))))
    use_flood, flood_note = resolve_flood(opts["flood"], sel)
    claimed, results = set(sel_set), []
    d = defaultdict(int)
    lim = max(eps, ltol * 2.0)
    budget = 400000

    for bi, (p1, p2, p3) in enumerate(bases):
        if len(results) >= opts["max_results"] or d["try"] > budget:
            break
        d12 = (p2.co - p1.co).length
        d13 = (p3.co - p1.co).length
        d23 = (p3.co - p2.co).length
        f1, f2, f3 = feats[p1], feats[p2], feats[p3]

        cand1 = [v for v in bm.verts if feat_eq(feats[v], f1, atol)]
        d["cand1"] += len(cand1)
        d["bases"] += 1

        for A in cand1:
            if len(results) >= opts["max_results"] or d["try"] > budget:
                break
            n2 = [bm.verts[i] for (_, i, dd) in kd.find_range(A.co, d12 + lim)
                  if dd >= d12 - lim]
            n2 = [v for v in n2 if feat_eq(feats[v], f2, atol)]
            if not n2:
                continue
            n3 = [bm.verts[i] for (_, i, dd) in kd.find_range(A.co, d13 + lim)
                  if dd >= d13 - lim]
            n3 = {v.index for v in n3 if feat_eq(feats[v], f3, atol)}
            if not n3:
                continue

            for B in n2:
                # третью точку берём пересечением двух сфер вместо перебора
                # всех пар — это снимает квадратичность (идея Super4PCS)
                for (_, ci, dd) in kd.find_range(B.co, d23 + lim):
                    if dd < d23 - lim or ci not in n3:
                        continue
                    C = bm.verts[ci]
                    d["try"] += 1
                    R, t, dev, mirror = rigid_fit(
                        [(p1.co, A.co), (p2.co, B.co), (p3.co, C.co)])
                    if dev > eps:
                        continue
                    if mirror and not opts["allow_mirror"]:
                        d["mirror"] += 1
                        continue
                    if not orient_ok(R, opts["orientation"], s_ang):
                        d["orient"] += 1
                        continue
                    got = surface_collect(bm, bvh_mesh, bvh_patch, sel, R, t,
                                          eps, cos_a, need_surf,
                                          opts["require_normal"], use_flood)
                    if got is None:
                        d["surf"] += 1
                        continue
                    faces, nfl = got
                    d["flood"] += nfl
                    if faces & claimed:
                        d["overlap"] += 1
                        continue
                    claimed |= faces
                    results.append((faces, R, t, mirror))

    info = (f"баз {d['bases']}/{len(bases)}, областей {len(regions)}, "
            f"старт. верш. {d['cand1']}, гипотез {d['try']} / отсев: "
            f"поверхн. {d['surf']}, ориент. {d['orient']}, зеркало {d['mirror']}, "
            f"пересеч. {d['overlap']} | дозал. {flood_note} +{d['flood']} гр., "
            f"поверхн. {need_surf}/{len(sel)}, "
            f"eps {eps:.4f} (float32 {noise:.4f} при |xyz| {mscale:.0f})")
    return sel, results, info


def search(bm, opts):
    bm.faces.ensure_lookup_table()
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    if opts["method"] == 'REGION':
        return search_regions(bm, opts)

    sel, results, info = search_faces(bm, opts)
    if opts["method"] != 'AUTO' or results:
        return sel, results, info
    # Обход по граням требует одинаковой топологии. Если копии переразбиты на
    # треугольники иначе (частый след экспортного конвейера), он не найдёт
    # ничего ни при каких порогах — переходим на поиск по областям.
    sel2, res2, info2 = search_regions(bm, opts)
    return sel2, res2, f"[грани] {info}  ==>  [области] {info2}"


METHOD_ITEMS = [
    ('AUTO', "Авто",
     "Сначала быстрый по граням; если он не нашёл ничего — по плоским "
     "областям. Покрывает и сшитую геометрию, и объекты с разной триангуляцией"),
    ('FAST', "Быстрый (по грани)", "Сигнатура = сама грань"),
    ('STRICT', "Строгий (+ двугранные углы)",
     "Дополнительно сверяет углы на рёбрах внутри фрагмента"),
    ('REGION', "По плоским областям",
     "Не зависит от разбиения на треугольники. Медленнее"),
]

UV_ITEMS = [
    ('ANY', "Не проверять", "Развёртка игнорируется"),
    ('SAME', "Точное совпадение",
     "UV должны совпадать абсолютно. Случай запечённых инстансов в общем UV"),
    ('SHIFT', "С точностью до сдвига",
     "UV-остров может быть сдвинут, но форма и размер те же"),
]

ORIENT_ITEMS = [
    ('ANY', "Любая", "Любой поворот"),
    ('Z', "Поворот вокруг Z", "Ось Z сохраняется"),
    ('SAME', "Только сдвиг", "Строго параллельный перенос"),
]


PRESETS = {
    'TIGHT': (0.001, 1.0, 1.0),
    'NORMAL': (0.005, 3.0, 1.0),
    'LOOSE': (0.020, 8.0, 0.9),
}


def _apply_preset(self, ctx):
    vals = PRESETS.get(self.preset)
    if vals:
        self.tol, self.angle_tol, self.min_match = vals


class SimilarPartsSettings(bpy.types.PropertyGroup):
    preset: bpy.props.EnumProperty(
        name="Точность", default='NORMAL', update=_apply_preset,
        items=[('TIGHT', "Точные копии", "Только побитово одинаковая геометрия"),
               ('NORMAL', "Обычная", "Рабочий вариант для большинства моделей"),
               ('LOOSE', "Мягкая",
                "Больше находок за счёт ложных: копии могут отличаться")],
        description="Задаёт допуск, допуск угла и долю совпадения. "
                    "Точные значения видны и правятся в блоке «Дополнительно»")
    show_advanced: bpy.props.BoolProperty(name="Дополнительно", default=False)
    """Общие настройки обоих операторов — живут в сцене, а не в операторе."""
    method: bpy.props.EnumProperty(name="Метод", items=METHOD_ITEMS, default='AUTO')
    tol: bpy.props.FloatProperty(
        name="Допуск", default=0.005, min=0.0, soft_max=1.0, max=10.0,
        step=1, precision=4, unit='LENGTH',
        description="Разрешённое расхождение длин рёбер и площадей при отборе "
                    "кандидатов. Больше — больше кандидатов и находок, но и "
                    "больше ложных совпадений и дольше поиск")
    angle_tol: bpy.props.FloatProperty(
        name="Допуск угла, °", default=3.0, min=0.0, max=90.0,
        description="Допуск для двугранных углов (строгий режим), сверки нормалей "
                    "и фильтра ориентации")
    planar_tol: bpy.props.FloatProperty(
        name="Порог плоскости, °", default=2.0, min=0.0, max=45.0,
        description="Соседние грани с меньшим двугранным углом склеиваются в одну "
                    "область. Поднимите, если геометрия слегка непланарная")
    min_match: bpy.props.FloatProperty(
        name="Доля совпадения", default=1.0, min=0.3, max=1.0, subtype='FACTOR',
        description="Какая доля полигонов фрагмента должна совпасть. 1.0 — только "
                    "полные совпадения. Снижайте, если копии почти идентичны, но "
                    "местами отличаются")
    base_tries: bpy.props.IntProperty(
        name="Базовых троек", default=6, min=1, max=30,
        description="Режим областей. Сколько разных стартовых троек вершин "
                    "перебрать. Одна тройка — одна точка отказа: если у её "
                    "первой вершины не нашлось соответствия, результат нулевой. "
                    "Больше — выше охват, пропорционально дольше поиск")
    rel_tol: bpy.props.FloatProperty(
        name="Порог посадки, % диагонали", default=0.0, min=0.0, max=10.0,
        description="Дополнительный порог посадки как доля габаритной диагонали "
                    "фрагмента. Не зависит от плотности сетки и от масштаба "
                    "модели. 0 = не использовать")
    anchor_tries: bpy.props.IntProperty(
        name="Проб якорей", default=4, min=1, max=20,
        description="Сколько разных стартовых граней пробовать. Больше — выше шанс "
                    "найти совпадения, пропорционально дольше поиск")
    eps_override: bpy.props.FloatProperty(
        name="Порог посадки", default=0.0, min=0.0, soft_max=1.0,
        step=1, precision=4, unit='LENGTH',
        description="0 = авто (max от допуска и шума float32)")
    ignore_extra: bpy.props.BoolProperty(
        name="Только главная часть выделения", default=False,
        description="Если выделение состоит из нескольких несвязных кусков, "
                    "проверять только самый крупный. Находок больше, точность ниже")
    use_parts: bpy.props.BoolProperty(
        name="Ускорение по отдельным объектам", default=True,
        description="Если выделен целый несвязный объект, искать только среди "
                    "объектов с тем же числом граней и площадью. Сильно ускоряет. "
                    "Отключите, если совпадения не находятся")
    require_normal: bpy.props.BoolProperty(
        name="Сверять нормали", default=True,
        description="Требовать совпадения направления нормалей при проверке по "
                    "поверхности. Отключение находит больше, но может цеплять "
                    "изнанку и совмещённые поверхности")
    flood: bpy.props.EnumProperty(
        name="Дозаливка", default='AUTO',
        items=[('AUTO', "Авто",
                "Включается, если выделены целиком отдельные объекты"),
               ('ON', "Всегда", "Всегда добирать грани цели по поверхности"),
               ('OFF', "Никогда", "Не добирать")],
        description="Добирать грани цели, целиком лежащие на исходном фрагменте. "
                    "Нужна, если цель разбита на треугольники иначе. Безопасна "
                    "для отдельных объектов и рискованна для куска, вваренного "
                    "в общую геометрию: там цепляет тонкие полигоны на границе")
    uv_mode: bpy.props.EnumProperty(
        name="Совпадение UV", items=UV_ITEMS, default='ANY',
        description="Дополнительное условие: совпадение развёртки. "
                    "Работает только в режимах по граням")
    uv_tol: bpy.props.FloatProperty(
        name="Допуск UV", default=0.001, min=0.0, soft_max=0.1,
        step=1, precision=5,
        description="В единицах UV (весь атлас = 1.0)")
    orientation: bpy.props.EnumProperty(
        name="Ориентация", items=ORIENT_ITEMS, default='ANY')
    allow_mirror: bpy.props.BoolProperty(
        name="Зеркальные", default=False,
        description="Принимать зеркальные копии. Для инстансов даёт отрицательный "
                    "масштаб — может быть проблемой при выгрузке")
    max_results: bpy.props.IntProperty(
        name="Максимум совпадений", default=500, min=1,
        description="Ограничитель: поиск останавливается, набрав столько совпадений")
    extend: bpy.props.BoolProperty(
        name="Оставить исходное выделение", default=True,
        description="Выключите, чтобы в выделении остались только найденные копии")


def opts_from(st):
    return {"method": st.method, "tol": st.tol, "angle_tol": st.angle_tol,
            "planar_tol": st.planar_tol, "orientation": st.orientation,
            "allow_mirror": st.allow_mirror, "max_results": st.max_results,
            "min_match": st.min_match, "anchor_tries": st.anchor_tries,
            "ignore_extra": st.ignore_extra, "use_parts": st.use_parts,
            "require_normal": st.require_normal, "eps_override": st.eps_override,
            "flood": st.flood, "uv_mode": st.uv_mode, "uv_tol": st.uv_tol,
            "base_tries": st.base_tries, "rel_tol": st.rel_tol}


def mat_from(R, t):
    return Matrix(((R[0][0], R[0][1], R[0][2], t[0]),
                   (R[1][0], R[1][1], R[1][2], t[1]),
                   (R[2][0], R[2][1], R[2][2], t[2]),
                   (0.0, 0.0, 0.0, 1.0)))


# кэш последнего поиска: имя меша -> результат
LAST = {}


def cache_store(me, bm, sel, results, info):
    LAST[me.name] = {
        "nfaces": len(bm.faces),
        "sel": frozenset(f.index for f in sel),
        "results": [(frozenset(f.index for f in faces), mat_from(R, t), mirror)
                    for faces, R, t, mirror in results],
        "info": info,
    }


def cache_load(me, bm):
    """Отдаёт результат последнего поиска, если меш и выделение не менялись."""
    c = LAST.get(me.name)
    if not c or c["nfaces"] != len(bm.faces):
        return None
    cur = frozenset(f.index for f in bm.faces if f.select)
    res = frozenset().union(*[s for s, _, _ in c["results"]]) if c["results"] else frozenset()
    if cur not in (c["sel"], c["sel"] | res, res):
        return None
    bm.faces.ensure_lookup_table()
    sel = [bm.faces[i] for i in sorted(c["sel"])]
    out = [([bm.faces[i] for i in sorted(s)], M, mirror)
           for s, M, mirror in c["results"]]
    return sel, out, c["info"]


# ---------------------------------------------------------------- operators

class MESH_OT_find_similar_parts(bpy.types.Operator):
    """Найти фрагменты меша, конгруэнтные выделенному набору полигонов.
    Настройки — на панели N > Similar"""
    bl_idname = "mesh.find_similar_parts"
    bl_label = "Find Similar Parts"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, ctx):
        return ctx.mode == 'EDIT_MESH' and ctx.object is not None

    def execute(self, ctx):
        st = ctx.scene.similar_parts
        me = ctx.object.data
        bm = bmesh.from_edit_mesh(me)
        sel, results, info = search(bm, opts_from(st))
        if not sel:
            self.report({'ERROR'}, "Нет выделенных полигонов")
            return {'CANCELLED'}

        cache_store(me, bm, sel, results, info)

        if not st.extend:
            for v in bm.verts:
                v.select_set(False)
            for e in bm.edges:
                e.select_set(False)
            for f in bm.faces:
                f.select_set(False)
        for faces, *_ in results:
            for f in faces:
                f.select_set(True)

        bm.select_flush(True)
        bmesh.update_edit_mesh(me)
        print("[Similar Parts]", info)
        self.report({'INFO'}, f"Совпадений: {len(results)} | {info}")
        return {'FINISHED'}


class MESH_OT_similar_parts_to_instances(bpy.types.Operator):
    """Вынести выделенный фрагмент в отдельный объект и заменить найденные
    совпадения инстансами (общий mesh data)"""
    bl_idname = "mesh.similar_parts_to_instances"
    bl_label = "Similar Parts to Instances"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, ctx):
        return ctx.mode == 'EDIT_MESH' and ctx.object is not None

    def execute(self, ctx):
        st = ctx.scene.similar_parts
        ob = ctx.object
        me = ob.data
        bm = bmesh.from_edit_mesh(me)

        src_info = "повторный поиск"
        got = cache_load(me, bm)
        if got:
            sel, results, info = got
            src_info = "результат последнего поиска"
        else:
            sel, raw, info = search(bm, opts_from(st))
            results = [(faces, mat_from(R, t), mirror)
                       for faces, R, t, mirror in raw]

        if not sel:
            self.report({'ERROR'}, "Нет выделенных полигонов")
            return {'CANCELLED'}
        if not results:
            self.report({'WARNING'}, f"Совпадений не найдено | {info}")
            return {'CANCELLED'}

        mats = [(M, mirror) for _, M, mirror in results]

        doomed = set()
        for faces, _, _ in results:
            doomed |= set(faces)
        bmesh.ops.delete(bm, geom=list(doomed), context='FACES')

        for v in bm.verts:
            v.select_set(False)
        for e in bm.edges:
            e.select_set(False)
        for f in bm.faces:
            f.select_set(False)
        for f in sel:
            if f.is_valid:
                f.select_set(True)
        bm.select_flush(True)
        bmesh.update_edit_mesh(me)

        before = set(ctx.view_layer.objects)
        bpy.ops.mesh.separate(type='SELECTED')
        bpy.ops.object.mode_set(mode='OBJECT')
        new = [o for o in ctx.view_layer.objects if o not in before]
        if not new:
            self.report({'ERROR'}, "Не удалось отделить фрагмент")
            return {'CANCELLED'}
        src = new[0]
        src.name = ob.name + "_part"
        src.data.name = src.name

        colls = list(src.users_collection) or [ctx.scene.collection]
        n_mirror = 0
        for M, mirror in mats:
            inst = bpy.data.objects.new(src.name, src.data)
            inst.matrix_world = ob.matrix_world @ M
            for c in colls:
                c.objects.link(inst)
            if mirror:
                n_mirror += 1

        LAST.pop(me.name, None)
        msg = f"Инстансов: {len(mats)} ({src_info}), донор: {src.name}"
        if n_mirror:
            msg += f"; зеркальных {n_mirror} (отрицательный масштаб)"
        self.report({'INFO'}, msg)
        return {'FINISHED'}


# ---------------------------------------------------------------- UI

class VIEW3D_PT_similar_parts(bpy.types.Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Similar"
    bl_label = "Similar Parts"

    @classmethod
    def poll(cls, ctx):
        return ctx.mode == 'EDIT_MESH'

    def draw(self, ctx):
        st = ctx.scene.similar_parts
        lay = self.layout

        lay.prop(st, "method")
        lay.prop(st, "preset")
        if st.method != 'REGION':
            lay.prop(st, "uv_mode")
        lay.prop(st, "orientation")
        lay.prop(st, "allow_mirror")

        box = lay.box()
        box.prop(st, "show_advanced", emboss=False,
                 icon='TRIA_DOWN' if st.show_advanced else 'TRIA_RIGHT')
        if st.show_advanced:
            col = box.column(align=True)
            col.prop(st, "tol")
            col.prop(st, "angle_tol")
            col.prop(st, "min_match")
            col.separator()
            col.prop(st, "flood")
            col.prop(st, "require_normal")
            col.prop(st, "ignore_extra")
            if st.method in ('FAST', 'STRICT', 'AUTO'):
                col.prop(st, "anchor_tries")
                col.prop(st, "use_parts")
            if st.method in ('REGION', 'AUTO'):
                col.prop(st, "base_tries")
                col.prop(st, "planar_tol")
            if st.method != 'REGION' and st.uv_mode != 'ANY':
                col.prop(st, "uv_tol")
            col.separator()
            col.prop(st, "eps_override")
            col.prop(st, "rel_tol")
            col.prop(st, "max_results")
            col.prop(st, "extend")

        lay.separator()
        lay.operator(MESH_OT_find_similar_parts.bl_idname,
                     text="Найти совпадения", icon='VIEWZOOM')

        c = LAST.get(ctx.object.data.name) if ctx.object else None
        col = lay.column(align=True)
        if c:
            col.label(text=f"Найдено: {len(c['results'])}, готово к замене",
                      icon='CHECKMARK')
        else:
            col.label(text="Поиск ещё не запускался", icon='INFO')
        col.operator(MESH_OT_similar_parts_to_instances.bl_idname,
                     text="Сделать инстансы", icon='LINKED')


def menu_func(self, ctx):
    self.layout.separator()
    self.layout.operator(MESH_OT_find_similar_parts.bl_idname,
                         text="Similar Parts (по выделению)")
    self.layout.operator(MESH_OT_similar_parts_to_instances.bl_idname,
                         text="Similar Parts to Instances")


classes = (SimilarPartsSettings, MESH_OT_find_similar_parts,
           MESH_OT_similar_parts_to_instances, VIEW3D_PT_similar_parts)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.similar_parts = bpy.props.PointerProperty(type=SimilarPartsSettings)
    bpy.types.VIEW3D_MT_select_edit_mesh.append(menu_func)


def unregister():
    bpy.types.VIEW3D_MT_select_edit_mesh.remove(menu_func)
    del bpy.types.Scene.similar_parts
    for c in reversed(classes):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    register()
