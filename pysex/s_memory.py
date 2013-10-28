#!/usr/bin/env python
import z3
import s_value
import copy
import collections
import logging

l = logging.getLogger("s_memory")
l.setLevel(logging.DEBUG)

addr_mem_counter = 0
var_mem_counter = 0
# Conventions used:
# 1) The whole memory is readable
# 2) Memory locations are by default writable
# 3) Memory locations are by default not executable

class Cell:
        # Type: RWX bits
        def __init__(self, ctype, cnt):
                self.type = ctype | 4 # memory has to be readable
                self.cnt = cnt

class MemDict(dict):
        def __init__(self, infobin={}):
                self.__infobin = dict(infobin)

        def __missing__(self, addr):
                global var_mem_counter
                sbin = None
                # look into the ghost memory
                for b in self.__infobin.itervalues():
                        r = b.get_range_addr()
                        if addr >= r[0] and addr <= r[1]:
                                sbin = b
                                break
                if sbin:
                        l.debug("Address %s is in ghost memory" %addr)
                        ida = sbin.get_ida()
                        sym_name = sbin.get_name_by_addr(addr)
                        if sym_name: # must solve the link
                                jmp_addr = self.__infobin[sbin[sym_name].extrn_lib_name][sym_name].addr
                                size = ida.idautils.DecodeInstruction(sbin[sym_name].addr).size * 8
                                assert  size >= jmp_addr.bit_length(), "Address inexpectedly too long"
                                cnt = z3.BitVecVal(jmp_addr, size)
                                for off in range(0, cnt.size() / 8):
                                        cell = Cell(5, z3.Extract((off << 3) + 7, (off << 3), cnt))
                                        self.__setitem__(addr + off, cell)

                        else:
                                self.__setitem__(addr, Cell(5, ida.idaapi.get_byte(addr)))
                else:
                        var = z3.BitVec("mem_%d" % var_mem_counter, 8)
                        var_mem_counter += 1
                        self.__setitem__(addr, Cell(6, var))

                return self.__getitem__(addr)

class Memory:
        def __init__(self, initial=None, infobin={}, sys=None):
                #TODO: copy-on-write behaviour
                self.__limit = 1024
                self.__bits = sys if sys else 64
                self.__max_mem = 2**self.__bits
                self.__freemem = [(0, self.__max_mem - 1)]
                self.__wrtmem =  [(0, self.__max_mem - 1)]
                self.__excmem =  []

                if infobin:
                        ghostmem = sorted([infobin[k].get_range_addr() for k in infobin.keys()])
                        keys = [[-1, 0]]  + ghostmem + [[self.__max_mem, self.__max_mem + 1]]
                        self.__freemem = [ j for j in [ ((keys[i][1] + 1, keys[i+1][0] - 1) if keys[i+1][0] - keys[i][1] > 1 else ()) for i in range(len(keys)-1) ] if j ]
                        self.__wrtmem = list(self.__freemem)
                        self.__excmem = list(self.__freemem)

                self.__mem = MemDict(infobin)

                if initial:
                        self.__mem.update(initial[0])
                        self.__update_info_mem(initial[1])

        def __update_info_mem(self, w_type):
                s_keys = sorted(self.__mem.keys())
                keys = [ -1 ] + s_keys + [ self.__max_mem ]
                if w_type & 2 or w_type & 1: # if the memory has been written
                        self.__freemem = [ j for j in [ ((keys[i] + 1, keys[i+1] - 1) if keys[i+1] - keys[i] > 1 else ()) for i in range(len(keys)-1) ] if j ]
                # updating writable memory
                if not w_type & 2: # the memory is marked as not re-writable
                        keys = [ -1 ] + [k for k in s_keys if not self.__mem[k].type & 2] + [ self.__max_mem ]
                        self.__wrtmem = [ j for j in [ ((keys[i] + 1, keys[i+1] - 1) if keys[i+1] - keys[i] > 1 else ()) for i in range(len(keys)-1) ] if j ]
                # updating executable memory
                if not w_type & 1: # the memory is marked as not executable
                        keys = [ -1 ] + [k for k in s_keys if not self.__mem[k].type & 1] + [ self.__max_mem ]
                        self.__excmem = [ j for j in [ ((keys[i] + 1, keys[i+1] - 1) if keys[i+1] - keys[i] > 1 else ()) for i in range(len(keys)-1) ] if j ]

        # def __getitem__(self, addr):
        #         return self.__mem[addr]

        def is_readable(self, addr):
                return self.__mem[addr].type & 4

        def is_writable(self, addr):
                return self.__mem[addr].type & 2

        def is_executable(self, addr):
                return self.__mem[addr].type & 1

        def __read_from(self, addr, num_bytes):
                # Check every addresses insted only the first one?
                if self.is_readable(addr):
                        if num_bytes == 1:
                                return self.__mem[addr].cnt
                        else:
                                return z3.Concat(*[self.__mem[addr + i].cnt for i in range( 0, num_bytes)])
                else:
                        l.warning("Attempted reading in a not readable location")
                        # FIX ME
                        return None

        def __write_to(self, addr, cnt, w_type=7):
                if self.is_writable(addr):
                        for off in range(0, cnt.size(), 8):
                                target = addr + off/8
                                new_content = z3.Extract(cnt.size() - off - 1, cnt.size() - off - 8, cnt)
                                new_perms = w_type | 4 # always readable
                                self.__mem[target] = Cell(new_perms, new_content)


                        # updating free memory
                        self.__update_info_mem(w_type)

                        return 1
                else:
                        l.info("Attempted writing in a not writable location")
                        return 0

        def store(self, dst, cnt, constraints, w_type=7):
                v = s_value.Value(dst, constraints)
                ret = []

                if v.is_unique():
                        # if there's only one option, let's do it
                        addr = v.any()
                else:
                        fcon = z3.Or([ z3.And(z3.UGE(dst,a), z3.ULE(dst,b)) for a,b in self.__freemem ])
                        v_free = s_value.Value(dst, constraints + [ fcon ])

                        if v_free.satisfiable():
                                # ok, found some memory!
                                # free memory is always writable
                                addr = v_free.any()
                                ret = [dst == addr]
                        else:
                                # ok, no free memory that this thing can address
                                fcon = z3.Or([ z3.And(z3.UGE(dst,a), z3.ULE(dst,b)) for a,b in self.__wrtmem ])
                                v_wrt = s_value.Value(dst, constraints + [ fcon ])
                                if v_wrt.satisfiable():
                                        addr = v_wrt.any()
                                        ret = [dst == addr]
                                else:
                                        print dst
                                        print constraints
                                        raise s_value.ConcretizingException("No memory expression %s can address." % dst)

                self.__write_to(addr, cnt, w_type)

                return ret

        #Load expressions from memory
        def load(self, dst, size, constraints=None):
                global addr_mem_counter
                expr = False
                ret = None
                size_b = size >> 3
                v = s_value.Value(dst, constraints)
                l.debug("Got load with size %d (%d bytes)" % (size, size_b))

                # specific read
                if v.is_unique():
                        addr = v.any()
                        expr = self.__read_from(addr, size/8)
                        expr = z3.simplify(expr)
                        ret = expr, [ ]

                elif abs(v.max() - v.min()) <= self.__limit:
                        # within the limit to keep it symbolic
                        fcon = z3.Or([ z3.And(z3.UGE(dst,a), z3.ULE(dst,b)) for a,b in self.__freemem ])
                        v_free = s_value.Value(dst, constraints + [ z3.Not(fcon) ])

                        # try to point it to satisfiable memory if possible
                        if v_free.satisfiable():
                                to_iterate = v_free
                        else:
                                to_iterate = v

                        var = z3.BitVec("%s_addr_%s" %(dst, addr_mem_counter), self.__bits)
                        addr_mem_counter += 1
                        for addr in to_iterate.iter():
                                cnc = self.__read_from(addr, size_b)
                                expr = z3.simplify(z3.Or(var == cnc, expr))

                        ret = expr, []
                else:
                        # too big, time to concretize!
                        if len(self.__mem):
                                #first try to point it somewhere valid
                                fcon = z3.Or([ dst == addr for addr in self.__mem.keys() ])
                                v_bsy = s_value.Value(dst, constraints + [ fcon ])

                                if v_bsy.satisfiable():
                                        addr = v_bsy.rnd()
                                else:
                                        addr = v.rnd() # at least the max value is included!

                                cnc = self.__read_from(addr, size_b)
                                cnc = z3.simplify(cnc)
                                ret = cnc, [dst == addr]
                        else:
                                # otherwise, concretize to a random location, just for fun
                                addr = v.rnd()
                                cnc = self.__read_from(addr, size_b)
                                cnc = z3.simplify(cnc)
                                ret = cnc, [dst == addr]

                return ret

        def get_bit_address(self):
                return self.__bits

        def pp(self):
                [l.debug("%d: [%s, %s]" %(addr, self.__mem[addr].cnt, self.__mem[addr].type)) for addr in self.__mem.keys()]

        def get_addresses(self):
                return self.__mem.keys()

        def get_max(self):
                return self.__max_mem

        #TODO: copy-on-write behaviour
        def copy(self):
                c = copy.copy(self)
                l.debug("Copying %d cells of memory." % len(c.__mem))
                c.__mem = copy.copy(c.__mem)
                return c
