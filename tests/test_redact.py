import pytest

from devops_agent.redact import redact, redact_object

# Fake values assembled at runtime so the repo itself contains no secret-shaped literals.
AWS = "AKIA" + "ABCDEFGHIJKLMNOP"
GITHUB = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
GOOGLE = "AIza" + "SyA1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q"
SLACK = "xoxb-" + "1234567890-abcdefghij"
STRIPE = "sk_live_" + "abcdefghijklmnop1234"
OPENAI = "sk-" + "abcdefghijklmnopqrstuvwx"
JWT = "eyJhbGciOiJI.eyJzdWIiOiIx.SflKxwRJSMeKKF2QT4"
PASSWORD = "Sup3rS3cretPw!"
# PEM markers are assembled at runtime so the repository holds no private-key-shaped text.
BEGIN_RSA = "-----BEGIN RSA " + "PRIVATE KEY-----"
END_RSA = "-----END RSA " + "PRIVATE KEY-----"
BEGIN_PLAIN = "-----BEGIN " + "PRIVATE KEY-----"


@pytest.mark.parametrize(
    "secret, kind",
    [
        (AWS, "aws-access-key"),
        (GITHUB, "github-token"),
        (GOOGLE, "google-api-key"),
        (SLACK, "slack-token"),
        (STRIPE, "stripe-key"),
        (OPENAI, "api-key"),
        (JWT, "jwt"),
    ],
)
def test_known_token_formats(secret, kind):
    out = redact(f"value is {secret} here")
    assert secret not in out
    assert f"[REDACTED:{kind}]" in out


def test_private_key_block_removed_including_body():
    block = BEGIN_RSA + "\nMIIEabc123\nxyz789\n" + END_RSA
    out = redact(f"before\n{block}\nafter")
    assert "MIIEabc123" not in out and "xyz789" not in out
    assert "before" in out and "after" in out


def test_truncated_private_key_still_redacted():
    out = redact(BEGIN_PLAIN + "\nMIIEabc123")
    assert "MIIEabc123" not in out


@pytest.mark.parametrize(
    "line",
    [
        f'password = "{PASSWORD}"',
        f"DB_PASSWORD={PASSWORD}",
        f"api_key: {PASSWORD}",
        f'"secret_token": "{PASSWORD}"',
        f"AUTH_TOKEN = '{PASSWORD}'",
    ],
)
def test_credential_assignments(line):
    out = redact(line)
    assert PASSWORD not in out
    assert "[REDACTED:credential]" in out


def test_assignment_keeps_name_for_location_context():
    assert redact(f"DB_PASSWORD={PASSWORD}").startswith("DB_PASSWORD=")


def test_url_password_redacted_user_kept():
    out = redact("postgres://admin:hunter2pass@db.example.com/app")
    assert "hunter2pass" not in out
    assert "admin" in out and "@db.example.com" in out


@pytest.mark.parametrize(
    "line",
    [
        "password: ${{ secrets.DB_PASSWORD }}",
        "token = os.environ['API_TOKEN_VALUE']",
        'api_key = os.getenv("API_KEY_VALUE")',
        "API_KEY=your_api_key_here",
        "SECRET=<your-secret-value>",
        "password = $DB_PASSWORD_ENV",
        "token: xxxxxxxxxxxx",
        "const token = process.env.GITHUB_TOKEN_VALUE",
    ],
)
def test_references_and_placeholders_are_kept(line):
    assert redact(line) == line


def test_short_values_not_treated_as_secrets():
    assert redact("password = 1234") == "password = 1234"


def test_extra_secret_removed_everywhere():
    out = redact("key=abc123SECRETxyz and again abc123SECRETxyz", extra_secrets=["abc123SECRETxyz"])
    assert "abc123SECRETxyz" not in out


def test_idempotent():
    once = redact(f"password = {PASSWORD} and {AWS}")
    assert redact(once) == once


def test_plain_text_unchanged():
    text = "def main():\n    print('hello world')\n"
    assert redact(text) == text


def test_non_string_passthrough():
    assert redact(None) is None
    assert redact("") == ""


def test_redact_object_recurses():
    obj = {"a": [f"x {AWS}", {"b": f"password={PASSWORD}"}], "n": 3}
    out = redact_object(obj)
    assert AWS not in str(out) and PASSWORD not in str(out)
    assert out["n"] == 3


@pytest.mark.parametrize(
    "line",
    [
        "username, password = get_auth_from_url(proxy)",
        "auth = HTTPBasicAuth(username, password=password)",
        "self.password = self.config_password",
        "password = user_supplied_password",
        'headers = {"password": password, "x": 1}',
        "token = request.session.token_value",
        "api_key = settings.API_KEY_VALUE",
        "secret_key = secrets[name_of_key]",
    ],
)
def test_code_expressions_are_not_mangled(line):
    assert redact(line) == line


@pytest.mark.parametrize(
    "line",
    [
        'password = "get_auth_from_url"',
        "PASSWORD=supersecretvalue",
        "password: Abc.12345xyz!",
        'password = "user_supplied_password"',
    ],
)
def test_literal_credentials_still_redacted(line):
    assert "[REDACTED:credential]" in redact(line)
