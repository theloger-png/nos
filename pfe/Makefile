# pfe/Makefile — NOS Packet Forwarding Engine
#
# Targets:
#   all      build nos-pfe binary and xdp/xdp_prog.o BPF object (default)
#   install  copy nos-pfe to /usr/local/sbin/, xdp_prog.o to /usr/lib/nos/
#   clean    remove build artefacts

# ── tools ─────────────────────────────────────────────────────────────────────
CC    ?= clang
CLANG := clang

# ── architecture detection ─────────────────────────────────────────────────────
# BPF programs compiled with -target bpf need an explicit path to the
# arch-specific kernel headers (asm/types.h, asm/byteorder.h, etc.) because
# clang does not pick up the host multiarch directory automatically when
# cross-targeting the BPF VM.
UNAME_M := $(shell uname -m)

ifeq ($(UNAME_M),x86_64)
  BPF_ARCH_INC := /usr/include/x86_64-linux-gnu
else ifeq ($(UNAME_M),aarch64)
  BPF_ARCH_INC := /usr/include/aarch64-linux-gnu
else
  $(warning Unrecognised arch '$(UNAME_M)'; BPF include path may be incorrect)
  BPF_ARCH_INC := /usr/include/$(UNAME_M)-linux-gnu
endif

# ── sources and outputs ────────────────────────────────────────────────────────
PFE_SRCS := main.c fib.c ipc.c xdp/xdp_loader.c
PFE_OBJS := $(PFE_SRCS:.c=.o)
PFE_DEPS := $(PFE_OBJS:.o=.d)
PFE_BIN  := nos-pfe

BPF_SRC  := xdp/xdp_prog.c
BPF_OBJ  := xdp/xdp_prog.o

# ── compiler / linker flags ────────────────────────────────────────────────────
CFLAGS   := -Wall -Wextra -O2 -g -I. -I./xdp

# Libraries needed by the PFE binary.
LDLIBS   := -lbpf -lcjson -lmnl -lpthread -lsystemd

# BPF compilation flags.
# -nostdinc strips the host C standard library but leaves compiler built-ins.
# -isystem re-adds clang's own built-in headers (for __builtin_*, integer
# width macros, etc.) and then the kernel + arch headers in the right order.
CLANG_BLTINS := $(shell $(CLANG) -print-file-name=include)

BPF_CFLAGS := \
  -O2 -g                              \
  -target bpf                         \
  -D__BPF_TRACING__                   \
  -nostdinc                           \
  -isystem $(CLANG_BLTINS)            \
  -I/usr/include                      \
  -I$(BPF_ARCH_INC)                   \
  -I.                                 \
  -I./xdp

# ── install paths ──────────────────────────────────────────────────────────────
DESTDIR   :=
SBINDIR   := $(DESTDIR)/usr/local/sbin
NOS_LIBDIR := $(DESTDIR)/usr/lib/nos

# ── phony targets ──────────────────────────────────────────────────────────────
.PHONY: all clean install

# ── default target ─────────────────────────────────────────────────────────────
all: $(BPF_OBJ) $(PFE_BIN)

# ── link PFE binary ────────────────────────────────────────────────────────────
$(PFE_BIN): $(PFE_OBJS)
	$(CC) -o $@ $^ $(LDLIBS)

# ── compile regular C sources ──────────────────────────────────────────────────
# -MMD -MP generates .d dependency files so that changes to headers trigger
# recompilation without manual tracking.
%.o: %.c
	$(CC) $(CFLAGS) -MMD -MP -c $< -o $@

# Pull in generated dependency files (silently if absent on first build).
-include $(PFE_DEPS)

# ── compile BPF program ────────────────────────────────────────────────────────
# Explicit rule so the BPF-specific flags are applied rather than the
# pattern rule above.  maps.h is listed as a prerequisite because all five
# map definitions live there.
$(BPF_OBJ): $(BPF_SRC) xdp/maps.h
	$(CLANG) $(BPF_CFLAGS) -c $< -o $@

# ── install ────────────────────────────────────────────────────────────────────
install: all
	install -d $(SBINDIR) $(NOS_LIBDIR)
	install -m 0755 $(PFE_BIN) $(SBINDIR)/$(PFE_BIN)
	install -m 0644 $(BPF_OBJ) $(NOS_LIBDIR)/xdp_prog.o

# ── clean ──────────────────────────────────────────────────────────────────────
clean:
	rm -f $(PFE_OBJS) $(PFE_DEPS) $(BPF_OBJ) $(PFE_BIN)
