PYTHON ?= python3
SRC    := src

.PHONY: help test lint run install clean

help:
	@echo "make test     运行单元测试"
	@echo "make lint     语法编译检查"
	@echo "make run      运行 f50sms doctor"
	@echo "make install  以可编辑方式安装（pip install -e .）"

test:
	PYTHONPATH=$(SRC) $(PYTHON) -m unittest discover -s tests -t . -v

lint:
	$(PYTHON) -m compileall -q $(SRC)/f50_sms tests && echo "compile ok"

run:
	./f50sms doctor

install:
	$(PYTHON) -m pip install -e .

clean:
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	rm -rf build dist src/*.egg-info
