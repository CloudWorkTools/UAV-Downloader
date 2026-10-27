"""Read-only native AST smoke check shared by image and production validation."""
import sys
from types import SimpleNamespace

from flow_checker import source_record


def signature(code):
    tree = SimpleNamespace(read=lambda path: code.encode())
    return source_record(tree, {'path': 'fixture.py', 'kind': 'python-module'})['semantic_hash']


def main():
    original = signature('def run():\n    return "a b"\n')
    assert signature('# ordinary comment\n\ndef run( ):\n    return ("a b")\n') == original
    assert signature('def run():\n    return "a  b"\n') != original
    assert signature('# noqa\ndef run():\n    return "a b"\n') != original
    directive = signature('first()  # noqa\nsecond()\n')
    assert signature('\nfirst()  # noqa\n\nsecond()\n') == directive
    assert signature('first()\nsecond()  # noqa\n') != directive
    type_ignore = signature('# type: ignore\nfirst()\nsecond()\n')
    assert signature('first()\n# type: ignore\nsecond()\n') != type_ignore
    try:
        signature('def run(:\n')
    except SyntaxError:
        pass
    else:
        raise AssertionError('Invalid AST must not be accepted')
    print('Native AST runtime smoke: PASS; Python ' + sys.version.split()[0])


if __name__ == '__main__':
    main()
