#!/bin/sh
# Try loading each campaign in a new python instance to try
# and fix the id_seq reset problem.  Must be run from the 
# loaders/CANON directory.  Argument is the stride.

python loadCANON_september2010.py 100 stoqs_september2010_s100
python loadCANON_october2010.py 100 stoqs_october2010_s100
python loadCANON_april2011.py 100 stoqs_april2011_s100
python loadCANON_june2011.py 100 stoqs_june2011_s100
