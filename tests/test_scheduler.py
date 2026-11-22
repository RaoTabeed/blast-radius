import unittest
from selector import schedule
class SchedulerTests(unittest.TestCase):
    def check(self, durations):
        tests = [{'number': i, 'seconds': value / 1000} for i, value in enumerate(durations)]
        workers = schedule(tests)
        self.assertEqual(sorted(n for w in workers for n in w), list(range(len(tests))))
        self.assertLessEqual(len(workers), 8)
        if tests:
            cost = max(sum(durations[n] for n in w) for w in workers) + 2000 * len(workers)
            self.assertLessEqual(cost, sum(durations) + 2000)
        return workers
    def test_empty(self):
        self.assertEqual(self.check([]), [])
    def test_short_tests(self):
        self.assertEqual(len(self.check([1] * 100)), 1)
    def test_long_tests(self):
        self.assertEqual(len(self.check([10000] * 8)), 8)
    def test_mixed(self):
        self.check([0, 0, 28820, 21450, 6000, 100, 1, 1, 5000])
if __name__ == '__main__':
    unittest.main()
