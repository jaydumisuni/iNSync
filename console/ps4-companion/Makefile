TITLE       := iNSync Companion
VERSION     := 1.00
TITLE_ID    := TTGI00001
CONTENT_ID  := IV0000-TTGI00001_00-INSYNCCOMPANION0

TOOLCHAIN   := $(OO_PS4_TOOLCHAIN)
INTDIR      := build
CDIR        := linux

LIBS        := -lc -lkernel -lc++ -lSceUserService -lSceVideoOut -lSceAudioOut -lScePad -lSceSysmodule -lSceFreeType -lSceBgft -lSceAppInstUtil -lSDL2
CXXFLAGS    := --target=x86_64-pc-freebsd12-elf -fPIC -funwind-tables -std=c++17 -c -isysroot $(TOOLCHAIN) -isystem $(TOOLCHAIN)/include -isystem $(TOOLCHAIN)/include/c++/v1 -O2 -Wall -Wextra -D_GNU_SOURCE -D_BSD_SOURCE
LDFLAGS     := -m elf_x86_64 -pie --script $(TOOLCHAIN)/link.x --eh-frame-hdr -L$(TOOLCHAIN)/lib $(LIBS) $(TOOLCHAIN)/lib/crt1.o

SRC         := $(wildcard src/*.cpp)
OBJ         := $(patsubst src/%.cpp,$(INTDIR)/%.o,$(SRC))

.PHONY: all prepare eboot package clean

all: eboot

prepare:
	mkdir -p $(INTDIR) sce_module sce_sys/about assets/fonts
	cp -f $(TOOLCHAIN)/samples/SDL2/sce_module/libc.prx sce_module/libc.prx
	cp -f $(TOOLCHAIN)/samples/SDL2/sce_module/libSceFios2.prx sce_module/libSceFios2.prx
	cp -f $(TOOLCHAIN)/samples/SDL2/sce_sys/about/right.sprx sce_sys/about/right.sprx
	cp -f $(TOOLCHAIN)/samples/threading/assets/fonts/Gontserrat-Regular.ttf assets/fonts/Gontserrat-Regular.ttf

$(INTDIR)/%.o: src/%.cpp
	mkdir -p $(INTDIR)
	clang++ $(CXXFLAGS) -o $@ $<

eboot: prepare $(OBJ)
	ld.lld $(OBJ) -o $(INTDIR)/companion.elf $(LDFLAGS)
	$(TOOLCHAIN)/bin/$(CDIR)/create-fself -in=$(INTDIR)/companion.elf -out=$(INTDIR)/companion.oelf --eboot "eboot.bin" --paid 0x3800000000000011

sce_sys/param.sfo:
	DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1 $(TOOLCHAIN)/bin/$(CDIR)/PkgTool.Core sfo_new $@
	DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1 $(TOOLCHAIN)/bin/$(CDIR)/PkgTool.Core sfo_setentry $@ APP_TYPE --type Integer --maxsize 4 --value 1
	DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1 $(TOOLCHAIN)/bin/$(CDIR)/PkgTool.Core sfo_setentry $@ APP_VER --type Utf8 --maxsize 8 --value '$(VERSION)'
	DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1 $(TOOLCHAIN)/bin/$(CDIR)/PkgTool.Core sfo_setentry $@ ATTRIBUTE --type Integer --maxsize 4 --value 0
	DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1 $(TOOLCHAIN)/bin/$(CDIR)/PkgTool.Core sfo_setentry $@ CATEGORY --type Utf8 --maxsize 4 --value 'gd'
	DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1 $(TOOLCHAIN)/bin/$(CDIR)/PkgTool.Core sfo_setentry $@ CONTENT_ID --type Utf8 --maxsize 48 --value '$(CONTENT_ID)'
	DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1 $(TOOLCHAIN)/bin/$(CDIR)/PkgTool.Core sfo_setentry $@ DOWNLOAD_DATA_SIZE --type Integer --maxsize 4 --value 0
	DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1 $(TOOLCHAIN)/bin/$(CDIR)/PkgTool.Core sfo_setentry $@ SYSTEM_VER --type Integer --maxsize 4 --value 0
	DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1 $(TOOLCHAIN)/bin/$(CDIR)/PkgTool.Core sfo_setentry $@ TITLE --type Utf8 --maxsize 128 --value '$(TITLE)'
	DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1 $(TOOLCHAIN)/bin/$(CDIR)/PkgTool.Core sfo_setentry $@ TITLE_ID --type Utf8 --maxsize 12 --value '$(TITLE_ID)'
	DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1 $(TOOLCHAIN)/bin/$(CDIR)/PkgTool.Core sfo_setentry $@ VERSION --type Utf8 --maxsize 8 --value '$(VERSION)'

pkg.gp4: eboot sce_sys/param.sfo sce_sys/icon0.png sce_sys/about/right.sprx sce_module/libc.prx sce_module/libSceFios2.prx assets/fonts/Gontserrat-Regular.ttf
	$(TOOLCHAIN)/bin/$(CDIR)/create-gp4 -out $@ --content-id=$(CONTENT_ID) --files "eboot.bin sce_sys/param.sfo sce_sys/icon0.png sce_sys/about/right.sprx sce_module/libc.prx sce_module/libSceFios2.prx assets/fonts/Gontserrat-Regular.ttf"

package: pkg.gp4
	DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1 $(TOOLCHAIN)/bin/$(CDIR)/PkgTool.Core pkg_build pkg.gp4 .

clean:
	rm -rf $(INTDIR) eboot.bin pkg.gp4 $(CONTENT_ID).pkg sce_sys/param.sfo sce_module assets/fonts
