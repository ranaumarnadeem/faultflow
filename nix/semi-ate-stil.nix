# Semi-ATE-STIL, an independent IEEE 1450 (STIL) parser. The python-tests
# check has it read every STIL file `ff.py write-patterns` makes
# (tests/python/scan_replay.py), so it's in pythonDevEnv. Test-only, and GPLv2:
# faultflow never imports it, and no faultflow package carries it.
#
# Not in nixpkgs. PyPI has only the sdist, whose setup.py doesn't declare the
# parser's run-time need for lark.
{
  buildPythonPackage,
  fetchPypi,
  setuptools,
  lark,
}:

buildPythonPackage rec {
  pname = "Semi-ATE-STIL";
  version = "0.3.2";
  pyproject = true;

  src = fetchPypi {
    inherit pname version;
    hash = "sha256-eJQVRMBb+lOuBD9GBTE3O9iZXqyVL604oymscFE5fFA=";
  };

  build-system = [ setuptools ];
  dependencies = [ lark ];

  # The sdist leaves its own tests out.
  doCheck = false;
  pythonImportsCheck = [ "Semi_ATE.STIL.parsers.STILParser" ];
}
