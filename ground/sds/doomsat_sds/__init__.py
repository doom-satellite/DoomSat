"""DoomSat's science data system: product code with no Airflow in it.

The DAGs in ../dags are thin wrappers around the functions here, so every product can be built and tested
without Airflow, Yamcs or the game. Nothing on the pilot side may import this package or read what it writes
(charter 2.2: no map survives an attempt); tests/test_sds_guard.py enforces that.
"""
__version__ = "0.1.0"
