class Mastapi:

    tag = ':mastapi:'

    @staticmethod
    def is_api_method(func):
        return None if func.__doc__ is None else Mastapi.tag in func.__doc__
