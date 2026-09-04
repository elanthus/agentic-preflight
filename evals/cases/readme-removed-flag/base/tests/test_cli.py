from greeter.cli import parser


def test_program_name():
    assert parser().prog == "toy-greet"
