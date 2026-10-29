#!/usr/bin/env python3
"""Allocate GPU memory on one card for a while, then free it and exit.

A stand-in for a borrower, for testing a lend end to end. It needs only the
CUDA runtime library (libcudart), reached through ctypes, so it runs in any
Python 3 with a CUDA driver installed. Under ``vram-lend run`` it reads the
card and the lent amount from the environment; the arguments override them.

    alloc-cuda.py [--gpu PCI] [--mib N] [--hold SECONDS] [--part FRACTION]

With no --mib it allocates PART (default 0.9) of VRAM_LEND_MIB. It prints
the device's free memory before, during and after, and exits non-zero when
the allocation fails.
"""

import argparse
import ctypes
import ctypes.util
import os
import sys
import time


def load_cudart():
    for name in ("libcudart.so", "libcudart.so.13", "libcudart.so.12",
                 ctypes.util.find_library("cudart") or ""):
        if not name:
            continue
        for base in ("", "/usr/local/cuda/lib64/"):
            try:
                return ctypes.CDLL(base + name)
            except OSError:
                continue
    sys.exit("alloc-cuda: libcudart not found; set LD_LIBRARY_PATH to the CUDA lib64 directory")


def check(rt, err, what):
    if err != 0:
        rt.cudaGetErrorString.restype = ctypes.c_char_p
        sys.exit(f"alloc-cuda: {what}: {rt.cudaGetErrorString(err).decode()} ({err})")


def free_mib(rt):
    free, total = ctypes.c_size_t(), ctypes.c_size_t()
    check(rt, rt.cudaMemGetInfo(ctypes.byref(free), ctypes.byref(total)), "cudaMemGetInfo")
    return free.value // (1024 * 1024), total.value // (1024 * 1024)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--gpu", default=os.environ.get("VRAM_LEND_GPU"), metavar="PCI")
    ap.add_argument("--mib", type=int, default=None, metavar="N")
    ap.add_argument("--hold", type=float, default=5.0, metavar="SECONDS")
    ap.add_argument("--part", type=float, default=0.9, metavar="FRACTION")
    args = ap.parse_args()
    if not args.gpu:
        sys.exit("alloc-cuda: no GPU: pass --gpu or run under vram-lend run")
    mib = args.mib
    if mib is None:
        lent = os.environ.get("VRAM_LEND_MIB")
        if not lent:
            sys.exit("alloc-cuda: no size: pass --mib or run under vram-lend run")
        mib = int(int(lent) * args.part)

    rt = load_cudart()
    device = ctypes.c_int()
    check(rt, rt.cudaDeviceGetByPCIBusId(ctypes.byref(device), args.gpu.encode()),
          f"cudaDeviceGetByPCIBusId {args.gpu}")
    check(rt, rt.cudaSetDevice(device), "cudaSetDevice")
    before = free_mib(rt)
    print(f"alloc-cuda: {args.gpu} is CUDA device {device.value}; free {before[0]} of {before[1]} MiB",
          flush=True)
    ptr = ctypes.c_void_p()
    size = ctypes.c_size_t(mib * 1024 * 1024)
    check(rt, rt.cudaMalloc(ctypes.byref(ptr), size), f"cudaMalloc {mib} MiB")
    check(rt, rt.cudaMemset(ptr, 1, size), "cudaMemset")
    during = free_mib(rt)
    print(f"alloc-cuda: holding {mib} MiB for {args.hold} s; free now {during[0]} MiB", flush=True)
    time.sleep(args.hold)
    check(rt, rt.cudaFree(ptr), "cudaFree")
    after = free_mib(rt)
    print(f"alloc-cuda: freed; free {after[0]} MiB", flush=True)


if __name__ == "__main__":
    main()
