class DecodeError(ValueError):
    pass

class ByteCursor:
    def __init__(self, data, start=0):
        self.data = memoryview(data)
        if not 0 <= start <= len(self.data):
            raise DecodeError('invalid cursor start')
        self.position = start

    def tell(self):
        return self.position

    @property
    def remaining(self):
        return len(self.data) - self.position

    def read_exact(self, count):
        if type(count) is not int or count < 0 or count > self.remaining:
            raise DecodeError('truncated or invalid byte length at ' + str(self.position))
        start = self.position
        self.position += count
        return self.data[start:self.position].tobytes()

    def skip_exact(self, count):
        if type(count) is not int or count < 0 or count > self.remaining:
            raise DecodeError('invalid skip at ' + str(self.position))
        self.position += count

    def require_end(self):
        if self.remaining:
            raise DecodeError('unconsumed payload: ' + str(self.remaining))
