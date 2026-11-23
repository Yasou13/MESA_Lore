"""R3 review regression tests: secret masking (no dev or holdout questions)."""
import json
import math
import os
import sys
from io import StringIO
from unittest.mock import patch

from mesa_lore import cli, mcp_server
from mesa_lore.store import Store
from mesa_lore.text import mask_secrets


def test_mask_more_secret_shapes():
    leaks = {
        '{"password": "hunter2hunter"}': "hunter2hunter",
        "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY": "wJalrXUtnFEMIK7",
        "DATABASE_URL=postgres://admin:S3cretPass@db.example.com/app": "S3cretPass",
        "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA": "b3BlbnNzaC1rZXk",
        "authorization: bearer abcdefghijklmnop": "abcdefghijklmnop",
        'client_secret: "abcd1234efgh"': "abcd1234efgh",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N": "dozjgNryP4J3",
        "HF_TOKEN=hf_abcdefghijklmnopqrstuvwxyz123456": "hf_abcdefghij",
    }
    for text, secret in leaks.items():
        assert secret not in mask_secrets(text), text
    # ordinary text stays readable
    for keep in ["max_tokens=4000", "refresh token süresi 7 gün", "https://example.com/a:b"]:
        assert mask_secrets(keep) == keep
