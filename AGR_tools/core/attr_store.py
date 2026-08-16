"""
Generic transport for a JSON record attached to a mesh object:
object custom property (idprop) + FBX-proof color-attribute mirror.

Extracted 1:1 from operators_link.py (AGR Link) and parameterised so
several namespaces (link / UDIM / atlas records) can coexist on one mesh:

- The record is a JSON dict stored in ``obj[prop_key]`` (fast, editable,
  saved in the .blend, exported as an FBX user property).
- The same dict is mirrored into color attributes ``<prefix>0..N``:
  zlib-compressed bytes, ONE byte per channel on the sRGB b/255 grid,
  framed by ``magic + version/flags + length(u32 LE) + crc32(u32 LE)``;
  version 2 appends ``n_loops(u32 LE)`` — the carrier mesh's loop count,
  which lets scan_windows() reconstruct a multi-layer blob EXACTLY even
  when an untracked block follows it after a plain Blender join (a v1
  window's end can only be guessed from the next magic candidate).
  The STANDARD FBX exporter carries vertex colors by default and the
  importer quantises them to BYTE_COLOR on the exact same sRGB grid, so a
  plain default File->Export/Import round trip preserves every byte.
  CRC-guarded: any corruption decodes to None, never to a
  plausible-but-wrong record.

The wire format and every guard below are shared with AGR Link containers
in the wild - do not change the framing without a version bump.  Readers
accept BOTH versions; writers emit version 2.
"""

import json
import zlib
from contextlib import contextmanager

import numpy as np

MAX_ATTRS = 128                  # capacity guard: 4 bytes per loop per attribute
MAX_PAYLOAD = 16 * 1024 * 1024   # sanity bound for the decoded length field
HEADER_V1 = 14                   # magic(4) + ver(1) + flags(1) + len(4) + crc(4)
HEADER_V2 = 18                   # v1 + n_loops(4): carrier loop count for exact windows


def read_srgb_bytes(attr):
    """Read a color attribute as bytes on the sRGB b/255 grid — identical
    for the FLOAT_COLOR we write and the BYTE_COLOR the FBX importer
    creates."""
    cnt = len(attr.data)
    arr = np.zeros(cnt * 4, dtype=np.float32)
    attr.data.foreach_get("color_srgb", arr)
    return np.clip(np.rint(arr.astype(np.float64) * 255.0), 0, 255).astype(np.uint8)


@contextmanager
def preserve_active_color(mesh):
    """Never leave a service layer as the mesh's active/render color: a
    Color Attribute node with a blank name would render packed bytes as
    vertex-colour noise.  Restores the captured layers, or CLEARS the
    active/default color when the mesh had none — assigning index -1
    silently bails in Blender's RNA setter (stderr, no exception), so the
    clear goes through ``active_color = None`` / ``default_color_name``
    instead (verified on 5.2).  Runs even when the wrapped block fails."""
    active_name = (mesh.color_attributes.active_color.name
                   if mesh.color_attributes.active_color else None)
    render_idx = mesh.color_attributes.render_color_index
    render_name = (mesh.color_attributes[render_idx].name
                   if 0 <= render_idx < len(mesh.color_attributes) else None)
    try:
        yield
    finally:
        if active_name is not None and mesh.color_attributes.get(active_name) is not None:
            mesh.color_attributes.active_color = mesh.color_attributes.get(active_name)
        else:
            try:
                mesh.color_attributes.active_color = None
            except (AttributeError, TypeError, ValueError, RuntimeError):
                pass
        if render_name is not None and mesh.color_attributes.get(render_name) is not None:
            for i, layer in enumerate(mesh.color_attributes):
                if layer.name == render_name:
                    mesh.color_attributes.render_color_index = i
                    break
        else:
            try:
                mesh.attributes.default_color_name = ""
            except (AttributeError, TypeError, ValueError, RuntimeError):
                pass


class ColorBlobStore:
    """One namespace of "JSON record on an object".

    ``validator`` guards BOTH read paths (idprop and colors) against
    foreign or corrupted records; ``idprop_exclude`` lists key PREFIXES
    kept out of the idprop copy (blob-only payloads, e.g. link's
    ``precise_*`` base64)."""

    def __init__(self, prefix, magic, prop_key, validator=None,
                 idprop_exclude=(), attr_type='FLOAT_COLOR',
                 version=2, flags=0, max_attrs=MAX_ATTRS):
        self.prefix = prefix
        self.magic = magic
        self.prop_key = prop_key
        self.validator = validator
        self.idprop_exclude = tuple(idprop_exclude)
        self.attr_type = attr_type
        self.version = version
        self.flags = flags
        self.max_attrs = max_attrs
        # poll()/draw() cache — external code holds aliases and mutates it
        # in place (pop/clear), so this dict is NEVER reassigned.
        self.cache = {}

    # ------------------------------------------------------------------
    # validation

    def _valid(self, record):
        if not isinstance(record, dict):
            return False
        if self.validator is None:
            return True
        try:
            return bool(self.validator(record))
        except Exception:
            return False

    def parse_idprop(self, raw):
        """The ONE parse/validate step shared by read() and peek()."""
        if not isinstance(raw, str):
            return None
        try:
            record = json.loads(raw)
        except (ValueError, TypeError):
            return None
        if not self._valid(record):
            return None
        return record

    # ------------------------------------------------------------------
    # color mirror (mesh level)

    def color_names(self, mesh):
        # digits-only filter: a foreign/duplicated name like "<prefix>0.001"
        # must not crash the sort - this runs inside poll()/draw()
        names = [a.name for a in mesh.attributes
                 if a.name.startswith(self.prefix)
                 and a.name[len(self.prefix):].isdigit()]
        return sorted(names, key=lambda n: int(n[len(self.prefix):]))

    def remove_mirror(self, mesh):
        """Remove ONLY this namespace's mirror attributes — so a stale
        mirror never contradicts the idprop record."""
        for name in self.color_names(mesh):
            attr = mesh.attributes.get(name)
            if attr is not None:
                mesh.attributes.remove(attr)

    def pack_colors(self, mesh, record):
        """Encode the record into ``<prefix>*`` color attributes: zlib
        bytes, ONE byte per channel on the sRGB b/255 grid (survives the
        FBX importer's BYTE_COLOR quantisation).  Returns False when the
        mesh cannot hold it — capacity is checked BEFORE the old mirror
        is removed."""
        n_loops = len(mesh.loops)
        if n_loops == 0:
            return False
        raw = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        payload = zlib.compress(raw, 9)
        header = (self.magic + bytes([self.version, self.flags])
                  + len(payload).to_bytes(4, "little")
                  + zlib.crc32(payload).to_bytes(4, "little"))
        if self.version >= 2:
            # carrier loop count: lets scan_windows() cut the exact window
            # after a plain Blender join (see module docstring)
            header += n_loops.to_bytes(4, "little")
        blob = header + payload
        per_attr = n_loops * 4  # 4 channels x 1 byte
        k = -(-len(blob) // per_attr)
        if k > self.max_attrs:
            return False
        with preserve_active_color(mesh):
            for name in self.color_names(mesh):
                attr = mesh.attributes.get(name)
                if attr is not None:
                    mesh.attributes.remove(attr)
            blob = blob.ljust(k * per_attr, b"\x00")
            floats = (np.frombuffer(blob, dtype=np.uint8).astype(np.float32)) / 255.0
            for i in range(k):
                attr = mesh.color_attributes.new(name=f"{self.prefix}{i}",
                                                 type=self.attr_type, domain='CORNER')
                chunk = floats[i * n_loops * 4:(i + 1) * n_loops * 4]
                attr.data.foreach_set("color_srgb", chunk)
        return True

    def _parse_header(self, blob):
        """Parse the frame header (both versions).  Returns
        (header_size, payload_length, crc, source_loops_or_None) or None
        when the bytes cannot be a valid frame."""
        if len(blob) < HEADER_V1 or blob[:4] != self.magic:
            return None
        version = blob[4]
        header = HEADER_V2 if version >= 2 else HEADER_V1
        if len(blob) < header:
            return None
        length = int.from_bytes(blob[6:10], "little")
        crc = int.from_bytes(blob[10:14], "little")
        if length <= 0 or length > MAX_PAYLOAD:
            return None
        src_loops = int.from_bytes(blob[14:18], "little") if version >= 2 else None
        return header, length, crc, src_loops

    def _decode_blob(self, blob):
        """Frame check + CRC + JSON + validator on a raw byte string.
        Shared by the whole-mesh decode and the window scanner."""
        parsed = self._parse_header(blob)
        if parsed is None:
            return None
        header, length, crc, _src = parsed
        if header + length > len(blob):
            return None
        payload = blob[header:header + length]
        if zlib.crc32(payload) != crc:
            return None
        try:
            record = json.loads(zlib.decompress(payload).decode("utf-8"))
        except (zlib.error, ValueError, UnicodeDecodeError):
            return None
        if not self._valid(record):
            return None
        return record

    def decode_colors(self, mesh):
        """Decode the record from ``<prefix>*`` color attributes (after an
        FBX round trip with default settings).  CRC-guarded: returns None
        on any corruption instead of a plausible-but-wrong record."""
        names = self.color_names(mesh)
        if not names:
            return None
        n_loops = len(mesh.loops)
        if n_loops == 0:
            return None
        chunks = []
        for name in names:
            attr = mesh.attributes.get(name)
            if attr is None or attr.domain != 'CORNER' or len(attr.data) != n_loops:
                return None
            chunks.append(read_srgb_bytes(attr))
        return self._decode_blob(np.concatenate(chunks).tobytes())

    def _candidate_offsets(self, t0):
        m = np.frombuffer(self.magic, dtype=np.uint8)
        return np.flatnonzero((t0[:, 0] == m[0]) & (t0[:, 1] == m[1])
                              & (t0[:, 2] == m[2]) & (t0[:, 3] == m[3])).tolist()

    def _first_layer_bytes(self, mesh):
        """(names, t0_bytes_reshaped) or (names, None) when unusable — the
        cheap shared prologue of scan_windows/count_window_candidates."""
        names = self.color_names(mesh)
        if not names:
            return names, None
        n_loops = len(mesh.loops)
        if n_loops == 0:
            return names, None
        first = mesh.attributes.get(names[0])
        if first is None or first.domain != 'CORNER' or len(first.data) != n_loops:
            return names, None
        return names, read_srgb_bytes(first).reshape(-1, 4)

    def count_window_candidates(self, mesh):
        """Cheap poll/draw gate: number of magic hits in the FIRST layer
        only — no other layers read, nothing decompressed.  A canonical
        carrier contributes exactly one hit at its own offset, so ``<= 1``
        means "no foreign windows to absorb"."""
        _names, t0 = self._first_layer_bytes(mesh)
        if t0 is None:
            return 0
        return len(self._candidate_offsets(t0))

    def scan_windows(self, mesh):
        """Find EVERY record window in the color layers.

        A plain Blender join concatenates same-named CORNER layers, each
        source mesh keeping its loops as one contiguous block — so the blob
        of every merged-in carrier survives at its own loop offset (layers
        the source lacked are zero-filled, which can never fake the magic).
        Candidates = magic hits on 4-byte (per-loop) boundaries of the first
        layer.  A v2 frame carries the carrier's loop count in its header,
        so the window is cut EXACTLY (a multi-layer blob reconstructs
        byte-perfectly even when an untracked block follows it).  Legacy v1
        frames fall back to guessing the end at the next candidate (or mesh
        end) with greedy extension past false in-payload magics — correct
        unless a non-carrier block follows a multi-layer v1 blob.
        Returns [(loop_start, loop_count, record), ...] ordered by
        loop_start; for v1 windows loop_count may include a zero tail of
        foreign loops — harmless for decoding, callers segment faces by it.
        """
        names, t0 = self._first_layer_bytes(mesh)
        if t0 is None:
            return []
        n_loops = len(mesh.loops)
        cand = self._candidate_offsets(t0)
        if not cand:
            return []
        layers = {names[0]: t0.ravel()}

        def layer_bytes(name):
            if name not in layers:
                attr = mesh.attributes.get(name)
                if attr is None or attr.domain != 'CORNER' or len(attr.data) != n_loops:
                    layers[name] = None
                else:
                    layers[name] = read_srgb_bytes(attr)
            return layers[name]

        def segment_blob(s, e):
            parts = []
            for name in names:
                b = layer_bytes(name)
                if b is None:
                    return None  # corrupt layer set
                parts.append(b[4 * s:4 * e])
            return np.concatenate(parts).tobytes()

        bounds = cand + [n_loops]
        windows = []
        i = 0
        while i < len(cand):
            s = cand[i]
            advanced = False
            # v2 frame: the header names the carrier's own loop count -
            # cut the exact window, no guessing
            head = self._parse_header(t0.ravel()[4 * s:4 * s + HEADER_V2 + 4].tobytes())
            if head is not None and head[3]:
                src_loops = head[3]
                if 0 < src_loops <= n_loops - s:
                    blob = segment_blob(s, s + src_loops)
                    record = self._decode_blob(blob) if blob is not None else None
                    if record is not None:
                        windows.append((s, src_loops, record))
                        # skip candidates swallowed by this exact window
                        i += 1
                        while i < len(cand) and cand[i] < s + src_loops:
                            i += 1
                        advanced = True
                if not advanced:
                    i += 1  # damaged v2 frame - the guess path can't help
                continue
            # v1 frame: guess the end at the next candidate, extend greedily
            for j in range(i + 1, len(bounds)):
                e = bounds[j]
                blob = segment_blob(s, e)
                if blob is None:
                    return windows
                record = self._decode_blob(blob)
                if record is not None:
                    windows.append((s, e - s, record))
                    i = j
                    advanced = True
                    break
            if not advanced:
                i += 1  # false magic (payload coincidence) - skip it
        return windows

    # ------------------------------------------------------------------
    # object level

    def read(self, obj):
        """Fresh, mutation-safe parse of the record (or None).  Falls back
        to the color-encoded record (fresh FBX import with default
        settings — no idprop yet), and then to the first window found by
        scan_windows() — a plain Blender join into a non-carrier active
        object keeps the mirror alive at a non-zero loop offset while the
        idprop dies with the carrier.  Operators use this; poll/draw must
        use the cached peek()."""
        record = self.parse_idprop(obj.get(self.prop_key))
        if record is None and getattr(obj, "type", None) == 'MESH':
            record = self.decode_colors(obj.data)
            if record is None:
                wins = self.scan_windows(obj.data)
                record = wins[0][2] if wins else None
        return record

    def write_idprop(self, obj, record):
        """idprop copy only — blob-only key prefixes (idprop_exclude) are
        kept out of the .blend / FBX user property."""
        lean = {k: v for k, v in record.items()
                if not any(k.startswith(p) for p in self.idprop_exclude)}
        obj[self.prop_key] = json.dumps(lean, ensure_ascii=False, separators=(",", ":"))

    def write(self, obj, record):
        """Full write: idprop first, then the color mirror.  When the mesh
        cannot hold the mirror, the idprop still carries the record inside
        the .blend and the stale mirror is removed so the two sources never
        contradict.  Returns True when the mirror was written."""
        self.write_idprop(obj, record)
        mesh = obj.data if getattr(obj, "type", None) == 'MESH' else None
        if mesh is None:
            return False
        ok = self.pack_colors(mesh, record)
        if not ok:
            self.remove_mirror(mesh)
        return ok

    def peek(self, obj):
        """Cached, poll()/draw()-safe read — never mutates ID data.
        Entries are validated against the raw idprop string (renames and
        edits never serve stale data); the colors path is O(1)-gated on
        the first mirror attribute so non-carriers stay cheap in poll()."""
        raw = obj.get(self.prop_key)
        if isinstance(raw, str):
            cached = self.cache.get(obj.name)
            if cached is not None and cached[0] == raw:
                return cached[1]
            record = self.parse_idprop(raw)
            if record is None:
                return None
            self.cache[obj.name] = (raw, record)
            return record
        # no idprop: maybe a colors-only carrier (fresh FBX import).  O(1)
        # gate on the first mirror attribute keeps non-carriers cheap.
        data = getattr(obj, "data", None)
        if data is None or data.attributes.get(self.prefix + "0") is None:
            return None
        fingerprint = ("colors", data.name, len(data.loops))
        cached = self.cache.get(obj.name)
        if cached is not None and cached[0] == fingerprint:
            return cached[1]
        record = self.decode_colors(data)
        if record is None:
            # mirror buried at a loop offset by a plain Blender join
            wins = self.scan_windows(data)
            record = wins[0][2] if wins else None
        self.cache[obj.name] = (fingerprint, record)
        return record

    def strip(self, obj):
        """Remove the record completely: idprop + mirror + cache entry."""
        obj.pop(self.prop_key, None)
        if getattr(obj, "type", None) == 'MESH':
            self.remove_mirror(obj.data)
        self.cache.pop(obj.name, None)

    def invalidate(self, name=None):
        if name is None:
            self.cache.clear()
        else:
            self.cache.pop(name, None)
