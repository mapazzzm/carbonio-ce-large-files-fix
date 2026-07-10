#!/usr/bin/env python3
"""
BlobService verifyBlobExists patch — replaces `ifne` (jump if true) with `ifge`
(jump if value >= 0). Since the boolean is always 0 or 1, both ≥ 0, the branch
is taken unconditionally — the throw block (DELETE node + DependencyException)
is skipped, letting uploads succeed even when verification falsely returns false.

Bug: BlobService.uploadFile calls verifyBlobExists() which downloads the just-
uploaded blob from storages to confirm presence. On large files, OkHttp client
in Java returns EOFException (storages closes idle TCP). verifyBlobExists
swallows it as `return false`, and uploadFile then throws DependencyException
"Upload verification failed: blob not accessible". Patch bypasses the throw.

Applies to: carbonio-files-ce 1.1.x and 1.2.x. The constant-pool index of the
verifyBlobExists Methodref differs between builds (#403 in 1.1.2, #399 in
1.2.4), so it is resolved dynamically from the class constant pool instead of
being hard-coded. Single byte change at 2 sites: opcode 0x9A → 0x9C.
"""
import zipfile, os, shutil, struct, sys, time

JAR = "/usr/share/carbonio/carbonio-files.jar"
CLASS_PATH = "com/zextras/carbonio/files/rest/services/BlobService.class"
METHOD = b"verifyBlobExists"

# install.sh creates its own backup before invoking this patch. Standalone
# users can opt in to a local backup by setting CU_BACKUP=1.
LOCAL_BACKUP = os.environ.get("CU_BACKUP") == "1"

# Constant-pool entry sizes by tag (JVMS §4.4); Utf8 (1) is variable-length.
_CP_FIXED = {3: 4, 4: 4, 5: 8, 6: 8, 7: 2, 8: 2, 9: 4, 10: 4, 11: 4,
             12: 4, 15: 3, 16: 2, 17: 4, 18: 4, 19: 2, 20: 2}


def _find_methodref(bs: bytes) -> int:
    """Return the constant-pool index of the Methodref for verifyBlobExists."""
    count = struct.unpack(">H", bs[8:10])[0]
    utf8_idx = None          # index of the Utf8 "verifyBlobExists"
    nat = {}                 # NameAndType index -> name index
    refs = {}                # Methodref index -> NameAndType index
    i, idx = 10, 1
    while idx < count:
        tag = bs[i]
        if tag == 1:
            ln = struct.unpack(">H", bs[i+1:i+3])[0]
            if bs[i+3:i+3+ln] == METHOD:
                utf8_idx = idx
            i += 3 + ln
        else:
            if tag == 12:
                nat[idx] = struct.unpack(">H", bs[i+1:i+3])[0]
            elif tag == 10:
                refs[idx] = struct.unpack(">H", bs[i+3:i+5])[0]
            i += 1 + _CP_FIXED[tag]
        idx += 2 if tag in (5, 6) else 1   # long/double take two slots
    if utf8_idx is None:
        sys.exit(f"{METHOD.decode()} not found in constant pool — method "
                 "renamed/removed in this carbonio-files-ce version. Aborting.")
    for ref, nat_idx in refs.items():
        if nat.get(nat_idx) == utf8_idx:
            return ref
    sys.exit(f"No Methodref for {METHOD.decode()} in constant pool. Aborting.")


def _patterns(bs: bytes):
    """(vanilla, patched) byte patterns for this class build."""
    ref = struct.pack(">H", _find_methodref(bs))
    return b"\xB6" + ref + b"\x9A", b"\xB6" + ref + b"\x9C"


def is_patched() -> bool:
    """True iff both verifyBlobExists sites already use ifge (0x9C)."""
    with zipfile.ZipFile(JAR, "r") as z:
        bs = bytearray(z.read(CLASS_PATH))
    pattern, patched = _patterns(bs)
    return bs.count(pattern) == 0 and bs.count(patched) >= 2


def main():
    if not os.path.exists(JAR):
        sys.exit(f"JAR not found: {JAR}")
    if "--check" in sys.argv:
        # Exit 0 if already patched, 1 otherwise. install.sh uses this to decide
        # whether the current JAR is vanilla and worth backing up.
        sys.exit(0 if is_patched() else 1)
    if LOCAL_BACKUP:
        bak = f"/root/backups/carbonio-files/{time.strftime('%Y%m%d_%H%M%S')}"
        os.makedirs(bak, exist_ok=True)
        shutil.copy2(JAR, bak)
        print(f"Backup: {bak}/carbonio-files.jar")

    with zipfile.ZipFile(JAR, "r") as z:
        bs = bytearray(z.read(CLASS_PATH))
    pattern, patched = _patterns(bs)
    print(f"  verifyBlobExists Methodref = #{struct.unpack('>H', pattern[1:3])[0]}")

    p, n = 0, 0
    while True:
        i = bs.find(pattern, p)
        if i < 0: break
        # pattern ends in 0x9A (ifne); finding it means this site is unpatched.
        bs[i+3] = 0x9C
        n += 1
        print(f"  offset {i}: 9A → 9C (ifne → ifge)")
        p = i + 1
    if n == 0:
        if bs.count(patched) >= 2:
            print("Already patched — nothing to do.")
            return
        sys.exit("Vanilla pattern not found and class is not patched — "
                 "bytecode layout changed. Aborting.")
    if n != 2:
        sys.exit(f"Expected 2 patch points, found {n}. Aborting.")

    new = JAR + ".new"
    with zipfile.ZipFile(JAR, "r") as zin, \
         zipfile.ZipFile(new, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zout:
        for item in zin.infolist():
            data = bytes(bs) if item.filename == CLASS_PATH else zin.read(item.filename)
            zout.writestr(item, data)
    os.replace(new, JAR)
    print(f"JAR repacked. Restart: systemctl restart carbonio-files")

if __name__ == "__main__":
    main()
