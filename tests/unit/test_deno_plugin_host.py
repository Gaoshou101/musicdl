import shutil,pytest
def test_deno_command_contract():
 if not shutil.which("deno"): pytest.skip("Deno executable unavailable")
 pytest.skip("runtime exercised in integration acceptance suite")
