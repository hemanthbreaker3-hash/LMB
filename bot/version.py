def get_version() -> str:
    MAJOR = "3"
    MINOR = "2"
    PATCH = "0"
    STATE = "lmb"
    return f"v{MAJOR}.{MINOR}.{PATCH}-{STATE}"


if __name__ == "__main__":
    print(get_version())
