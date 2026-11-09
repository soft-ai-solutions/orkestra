"""common: strings shared by several areas (English + Turkish). Keys are `common.<name>`; both dicts must hold the same
keys and {fields}."""
from __future__ import annotations

from typing import Dict

EN: Dict[str, str] = {
    "common.ago": "{dur} ago",
    "common.lock_busy": "{name} is busy: waited {waited} s, held by {holder}. Wait for it to finish, or stop that "
                        "process if it is stuck.",
    "common.lock_holder": "pid {pid} ({cmd}) for {held} s",
    "common.lock_holder_unknown": "another process",
    "common.office_hours": "Orkestra refuses commit and push on weekdays from 09:00 to 18:00 GMT+3. "
                           "Try again at 18:00 or later, or on Saturday or Sunday.",
}

TR: Dict[str, str] = {
    "common.ago": "{dur} önce",
    "common.lock_busy": "{name} meşgul: {waited} sn beklendi, tutan: {holder}. Bitmesini bekleyin; takıldıysa o "
                        "işlemi durdurun.",
    "common.lock_holder": "pid {pid} ({cmd}), {held} sn",
    "common.lock_holder_unknown": "başka bir işlem",
    "common.office_hours": "Orkestra hafta içi saat 09:00 ile 18:00 GMT+3 arasında commit ve push kabul etmez. "
                           "18:00 ve sonrasında ya da cumartesi veya pazar yeniden deneyin.",
}
