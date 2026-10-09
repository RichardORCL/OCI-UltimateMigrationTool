"""The Proxmox installer init script: detect the narrow disk scan and widen it."""

from __future__ import annotations

import pytest

from helper_app.oci.proxmox_iso import (
    MARKER,
    ProxmoxIsoError,
    fixed_object_name,
    inspect_init_script,
    patch_init_script,
    product_label,
    read_cd_info,
)

# The stock scan from the Proxmox VE installer initrd (tabs, as shipped).
_STOCK = """\
echo "searching for block device containing the ISO $ISONAME-$RELEASE-$ISORELEASE"
reqid="$(cat "/$CDID_FN")"
echo "with ISO ID '$reqid'"
delay=1
for try in $(seq 1 9); do
	for i in /sys/block/hd* /sys/block/sr* /sys/block/scd* /sys/block/sd* /sys/block/nvme*; do
		if [ -d "$i" ]; then
			basedev="${i##*/}"
			path="/dev/$basedev"
			size="$(cat "$i/size")"

			if [ "$(cat "$i/removable")" = 1 ] ||
				 blkid "$path" | grep -q ' TYPE="iso9660"' ||
				 [ "$size" -lt $(( 1024 * 1024 * 65 )) ]
			then
				echo "testing device '$path' for ISO"
				if mount -t auto -o ro "$path" /mnt >/dev/null 2>&1; then
					if [ -r "/mnt/$CDID_FN" ] && [ "X$(cat "/mnt/$CDID_FN")" = "X$reqid" ]; then
						echo "found $PRODUCTLONG ISO"
						cdrom=$path
						break
					fi
					umount /mnt
				fi
			fi
		fi
	done
	if test -n "$cdrom"; then
		break;
	fi
done
"""


def test_stock_init_needs_the_wider_scan():
    scan = inspect_init_script(_STOCK)
    assert scan.state == "needs_fix"


def test_patch_scans_virtio_disks_and_partitions_and_keeps_the_retry_loop():
    patched = patch_init_script(_STOCK)
    assert MARKER in patched
    assert "/sys/block/vd*" in patched and "/sys/block/mmcblk*" in patched
    assert "1024 * 1024 * 65" not in patched  # the size filter is gone
    assert 'testing device \'$devpath\' for ISO' in patched
    assert "[ -b \"$devpath\" ]" in patched
    # the outer retry, and the break once a disk matched, stay around the replaced loop
    assert "for try in $(seq 1 9)" in patched
    assert 'if test -n "$cdrom"' in patched
    assert inspect_init_script(patched).state == "already_fixed"
    # a second pass does not nest another loop
    assert patch_init_script(patched) == patched.replace("\r\n", "\n")
    assert patched.count("for i in /sys/block/") == 1


def test_other_init_scripts_are_refused():
    text = "#!/bin/sh\necho hello\n"
    assert inspect_init_script(text).state == "not_proxmox"
    with pytest.raises(ProxmoxIsoError, match="not a Proxmox installer"):
        patch_init_script(text)


def test_a_proxmox_init_with_an_unknown_scan_is_refused():
    text = 'echo "searching for block device containing the ISO $ISONAME"\n# no device loop\n'
    assert inspect_init_script(text).state == "unrecognised"
    with pytest.raises(ProxmoxIsoError, match="not the version"):
        patch_init_script(text)


def test_fixed_object_name_sits_beside_the_original():
    assert fixed_object_name("images/proxmox-ve_9.2-1.iso") == "images/proxmox-ve_9.2-1.oci-umt.iso"
    assert fixed_object_name("images/proxmox-ve_9.2-1.oci-umt.iso") == "images/proxmox-ve_9.2-1.oci-umt.iso"


def test_cd_info_label():
    info = read_cd_info("PRODUCTLONG='Proxmox VE'\nRELEASE='9.2'\nISORELEASE='1'\n")
    assert product_label(info) == "Proxmox VE 9.2-1"
