import angr
import resource
import time

import os


b = angr.Project(os.path.join(
    os.path.dirname(__file__),
    "../../binaries-private/cgc_scored_event_2/cgc/0b32aa01_01"
))

start = time.time()
#s = b.factory.blank_state(add_options={"COMPOSITE_SOLVER"})
s = b.factory.blank_state(add_options={"COMPOSITE_SOLVER"}, remove_options={"LAZY_SOLVES"})
sm = b.factory.simgr(s)
angr.simgr.l.setLevel("DEBUG")
sm.step(300)
end = time.time()
print "MB:", resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024
print "time:", end-start
#assert len(sm.active) == 1538
#assert len(sm.deadended) == 27
