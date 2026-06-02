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

Applies to: carbonio-files-ce 1.1.2 (carbonio-storages-ce 1.0.16).
Single byte change at 2 sites: opcode 0x9A → 0x9C.
"""
import zipfile, os, shutil, sys, time

JAR = "/usr/share/carbonio/carbonio-files.jar"
CLASS_PATH = "com/zextras/carbonio/files/rest/services/BlobService.class"
PATTERN = bytes.fromhex("B601939A")  # invokevirtual #403 verifyBlobExists + ifne

# install.sh creates its own backup before invoking this patch. Standalone
# users can opt in to a local backup by setting CU_BACKUP=1.
LOCAL_BACKUP = os.environ.get("CU_BACKUP") == "1"

def main():
    if not os.path.exists(JAR):
        sys.exit(f"JAR not found: {JAR}")
    if LOCAL_BACKUP:
        bak = f"/root/backups/carbonio-files/{time.strftime('%Y%m%d_%H%M%S')}"
        os.makedirs(bak, exist_ok=True)
        shutil.copy2(JAR, bak)
        print(f"Backup: {bak}/carbonio-files.jar")

    with zipfile.ZipFile(JAR, "r") as z:
        bs = bytearray(z.read(CLASS_PATH))

    p, n = 0, 0
    while True:
        i = bs.find(PATTERN, p)
        if i < 0: break
        if bs[i+3] == 0x9C:
            print(f"  offset {i}: already patched")
        else:
            bs[i+3] = 0x9C
            n += 1
            print(f"  offset {i}: 9A → 9C (ifne → ifge)")
        p = i + 1
    if n == 0:
        print("Nothing to patch (already done or version differs).")
        return
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
