import os
import tempfile

if not 'PROMETHEUS_MULTIPROC_DIR' in os.environ:
    os.environ['PROMETHEUS_MULTIPROC_DIR'] = tempfile.mkdtemp()

from prometheus_client import samples

class WrappedSample(samples.Sample):
    def __new__(cls, name, labels, value, timestamp=None, exemplar=None):
        labels = labels.copy()
        labels.pop("pid", None)
        return super().__new__(cls, name, labels, value, timestamp, exemplar)
samples.Sample = WrappedSample

from app import app


from prometheus_flask_exporter.multiprocess import GunicornInternalPrometheusMetrics

def child_exit(server, worker):
    GunicornInternalPrometheusMetrics.mark_process_dead_on_child_exit(worker.pid)

if __name__ == '__main__':
    app.run()