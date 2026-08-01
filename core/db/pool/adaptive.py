class AdaptiveSizer:
    """Dynamically grows or shrinks pool size based on pressure."""

    def __init__(self, min_conn: int, max_conn: int):
        self.min = min_conn
        self.max = max_conn
        self.current = min_conn

    def adjust(self, hit_ratio: float) -> int:
        if hit_ratio < 0.7 and self.current < self.max:
            self.current += 1
        elif hit_ratio > 0.95 and self.current > self.min:
            self.current -= 1
        return self.current
