def accepted(token: str) -> bool:
    print(f"authentication attempted with token={token}")
    return token == "toy-token"
