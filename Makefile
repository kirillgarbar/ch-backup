VERSION=1.0
TEST_VENV=.tox/py36
REQUIREMENTS_VENV=.tox/requirements
TEST_REQUIREMENTS_VENV=.tox/test_requirements
SESSION_FILE=.session_conf.sav
INSTALL_DIR=$(DESTDIR)/opt/yandex/ch-backup

CLICKHOUSE_VERSIONS?=1.1.54385 18.5.1 18.12.17 18.14.19 19.1.16 19.3.9 19.4.3.11
CLICKHOUSE_VERSION?=$(lastword $(CLICKHOUSE_VERSIONS))
export CLICKHOUSE_VERSION

export PYTHONIOENCODING?=utf8


.PHONY: build
build: ch_backup/version.txt


.PHONY: all
all: build lint unit_test integration_test


.PHONY: test
test: lint unit_test


.PHONY: lint
lint: build
	git --no-pager diff HEAD~1 --check
	tox -e isort,yapf,flake8,pylint,mypy,bandit


.PHONY: unit_test
unit_test: build
	tox -e unit_test


.PHONY: integration_test
integration_test: build create_env
	tox -e integration_test -- -D skip_setup


.PHONY: integration_test_all
integration_test_all: build
	@for version in $(CLICKHOUSE_VERSIONS); do \
		CLICKHOUSE_VERSION=$$version tox -e integration_test; \
	done


.PHONY: clean
clean: clean_env clean_pycache
	rm -rf .tox .cache *.egg-info htmlcov .coverage* .hypothesis ch_backup/version.txt

.PHONY: clean_pycache
clean_pycache:
	find . -name __pycache__ -type d -exec rm -rf {} +


.PHONY: install
install:
	@echo "Installing into $(INSTALL_DIR)"
	python3.6 -m venv $(INSTALL_DIR)
	$(INSTALL_DIR)/bin/pip install -r requirements.txt
	$(INSTALL_DIR)/bin/pip install .
	mkdir -p $(DESTDIR)/usr/bin/
	ln -s /opt/yandex/ch-backup/bin/ch-backup $(DESTDIR)/usr/bin/
	mkdir -p $(DESTDIR)/etc/bash_completion.d/
	env LC_ALL=C.UTF-8 LANG=C.UTF-8 \
	    _CH_BACKUP_COMPLETE=source $(INSTALL_DIR)/bin/ch-backup > $(DESTDIR)/etc/bash_completion.d/ch-backup || \
	    test -s $(DESTDIR)/etc/bash_completion.d/ch-backup
	rm -rf $(INSTALL_DIR)/bin/activate*
	find $(INSTALL_DIR) -name __pycache__ -type d -exec rm -rf {} +
	test -n '$(DESTDIR)' \
	    && grep -l -r -F '#!$(INSTALL_DIR)' $(INSTALL_DIR) \
	        | xargs sed -i -e 's|$(INSTALL_DIR)|/opt/yandex/ch-backup|' \
	    || true


.PHONY: uninstall
uninstall:
	@echo "Uninstalling from $(INSTALL_DIR)"
	rm -rf $(INSTALL_DIR) $(DESTDIR)/usr/bin/ch-backup $(DESTDIR)/etc/bash_completion.d/ch-backup


.PHONY: debuild
debuild: build debian_changelog
	cd debian && \
	    debuild --check-dirname-level 0 --no-tgz-check --preserve-env -uc -us

.PHONY: debian_changelog
debian_changelog:
	@rm -f debian/changelog
	dch --create --package ch-backup --distribution trusty \
	    -v `cat ch_backup/version.txt` \
	    "Yandex autobuild"


.PHONY: clean_debuild
clean_debuild: clean
	rm -rf debian/{changelog,files,ch-backup*}
	rm -f ../ch-backup_*{build,changes,deb,dsc,tar.gz}


.PHONY: create_env
create_env: build ${TEST_VENV} ${SESSION_FILE}

${SESSION_FILE}:
	PATH=${TEST_VENV}/bin:$$PATH ${TEST_VENV}/bin/python -m tests.integration.env_control create


.PHONY: start_env
start_env: create_env
	PATH=${TEST_VENV}/bin:$$PATH ${TEST_VENV}/bin/python -m tests.integration.env_control start


.PHONY: stop_env
stop_env:
	test -d ${TEST_VENV}/bin && test -f ${SESSION_FILE} && \
	PATH=${TEST_VENV}/bin:$$PATH ${TEST_VENV}/bin/python -m tests.integration.env_control stop || true


.PHONY: clean_env
clean_env: stop_env
	rm -rf staging ${SESSION_FILE}


.PHONY: format
format: ${TEST_VENV}
	${TEST_VENV}/bin/isort --recursive --apply ch_backup tests
	${TEST_VENV}/bin/yapf --recursive --parallel --in-place ch_backup tests


.PHONY: generate_requirements
generate_requirements: ${REQUIREMENTS_VENV}
	echo "# Generated by make generate_requirements" > requirements.txt
	${REQUIREMENTS_VENV}/bin/pip freeze | tee -a requirements.txt


.PHONY: generate_test_requirements
generate_test_requirements: ${TEST_REQUIREMENTS_VENV}
	echo "# Generated by make generate_tes_requirements" > requirements-test.txt
	${TEST_REQUIREMENTS_VENV}/bin/pip freeze | tee -a requirements-test.txt


.tox/%:
	tox -r -e $* --notest

${TEST_VENV}:
	tox -r -e integration_test --notest

${REQUIREMENTS_VENV}: requirements.in.txt

${TEST_REQUIREMENTS_VENV}: requirements.txt requirements-test.in.txt


ch_backup/version.txt:
	@echo ${VERSION}.`git rev-list HEAD --count` > ch_backup/version.txt


.PHONY: help
help:
	@echo "Targets:"
	@echo "  build (default)            Build project (it only generates version.txt for now)."
	@echo "  all                        Alias for \"build lint unit_test integration_test\"."
	@echo "  test                       Alias for \"lint unit_test\"."
	@echo "  lint                       Run linter tools."
	@echo "  unit_test                  Run unit tests."
	@echo "  integration_test           Run integration tests."
	@echo "  integration_test_all       Run integration tests against all supported versions of ClickHouse."
	@echo "  clean                      Clean up build and test artifacts."
	@echo "  create_env                 Create test environment."
	@echo "  start_env                  Start test environment runtime."
	@echo "  stop_env                   Stop test environment runtime."
	@echo "  clean_env                  Clean up test environment."
	@echo "  debuild                    Build Debian package."
	@echo "  clean_debuild              Clean up build and test artifacts including ones produced by"
	@echo "                             debuild target outside the project worksapce."
	@echo "  format                     Re-format source code to conform style settings enforced by"
	@echo "                             isort and yapf tools."
	@echo "  generate_requirements      Re-generate requirements.txt from requirements.in.txt."
	@echo "  help                       Show this help message."
	@echo
	@echo "Environment Variables:"
	@echo "  CLICKHOUSE_VERSION         ClickHouse version to use in integration_test target (default: \"$(CLICKHOUSE_VERSION)\")."
	@echo "  CLICKHOUSE_VERSIONS        List of ClickHouse versions to use in integration_test_all target"
	@echo "                             (default: \"$(CLICKHOUSE_VERSIONS)\")."
