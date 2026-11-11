# syntax=docker/dockerfile:1
# ocp-upi toolbox: every tool the project needs, pinned, in one image. Nothing is installed on the Docker host.
# Fedora base because coreos-installer (ISO customization) is packaged there.
# openshift-install/oc are per OpenShift release and per cluster, so the `tools` role downloads them into
# .state/bin/<release>/ at run time instead of baking them in.

ARG FEDORA_IMAGE=registry.fedoraproject.org/fedora:44

# --- static binaries --------------------------------------------------------
FROM ${FEDORA_IMAGE} AS bins
ARG TOFU_VERSION=1.13.1
# govc: vCenter SSO users (sso.user.*), which no Ansible module manages (MTV's vCenter account).
ARG GOVC_VERSION=0.56.2
# helm: charts that ship only as Helm charts (Portworx Backup, px-central), driven by kubernetes.core.helm.
ARG HELM_VERSION=3.22.1
RUN dnf -y install --setopt=install_weak_deps=False curl unzip tar gzip && dnf clean all
WORKDIR /dl
RUN set -eux; \
    curl -fsSLO "https://github.com/opentofu/opentofu/releases/download/v${TOFU_VERSION}/tofu_${TOFU_VERSION}_linux_amd64.zip"; \
    curl -fsSLO "https://github.com/opentofu/opentofu/releases/download/v${TOFU_VERSION}/tofu_${TOFU_VERSION}_SHA256SUMS"; \
    sha256sum -c --ignore-missing "tofu_${TOFU_VERSION}_SHA256SUMS"; \
    mkdir -p /out; unzip -o "tofu_${TOFU_VERSION}_linux_amd64.zip" tofu -d /out; \
    curl -fsSLO "https://github.com/vmware/govmomi/releases/download/v${GOVC_VERSION}/govc_Linux_x86_64.tar.gz"; \
    curl -fsSL -o govc_checksums.txt "https://github.com/vmware/govmomi/releases/download/v${GOVC_VERSION}/checksums.txt"; \
    sha256sum -c --ignore-missing govc_checksums.txt; \
    tar -xzf govc_Linux_x86_64.tar.gz -C /out govc; \
    curl -fsSLO "https://get.helm.sh/helm-v${HELM_VERSION}-linux-amd64.tar.gz"; \
    curl -fsSLO "https://get.helm.sh/helm-v${HELM_VERSION}-linux-amd64.tar.gz.sha256sum"; \
    sha256sum -c "helm-v${HELM_VERSION}-linux-amd64.tar.gz.sha256sum"; \
    tar -xzf "helm-v${HELM_VERSION}-linux-amd64.tar.gz" --strip-components=1 -C /out linux-amd64/helm; \
    chmod 0755 /out/*

# --- toolbox ----------------------------------------------------------------
FROM ${FEDORA_IMAGE}
ARG COREOS_INSTALLER_VERSION=0.26.0
LABEL org.opencontainers.image.title="ocp-upi-toolbox"
# coreos-installer: ISO customization. iputils: the preflight "node IP is free" ping. openssh-clients: Ansible to
# the infra host. git/tar/unzip/gzip: Ansible unarchive and Galaxy.
RUN dnf -y install --setopt=install_weak_deps=False \
      "coreos-installer-${COREOS_INSTALLER_VERSION}" \
      python3 python3-pip openssh-clients git tar unzip gzip iputils ca-certificates \
 && dnf clean all
COPY --from=bins /out/ /usr/local/bin/
COPY requirements.txt requirements.yml /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements.txt
# Collections go to the system-wide path, never to a home directory.
ENV ANSIBLE_COLLECTIONS_PATH=/usr/share/ansible/collections
RUN ansible-galaxy collection install -r /tmp/requirements.yml -p /usr/share/ansible/collections \
 && rm -rf /root/.ansible
# ssh needs a passwd entry for the runtime uid; HOME points into the bind-mounted ./.state
ARG TOOLBOX_UID=1000
ARG TOOLBOX_GID=1000
RUN groupadd -g ${TOOLBOX_GID} toolbox \
 && useradd -u ${TOOLBOX_UID} -g ${TOOLBOX_GID} -M -d /work/.state/home -s /bin/bash toolbox
WORKDIR /work
CMD ["bash"]
