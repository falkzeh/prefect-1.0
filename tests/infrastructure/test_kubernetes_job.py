import json
from contextlib import contextmanager
from pathlib import Path
from typing import Dict
from unittest import mock
from unittest.mock import MagicMock

import anyio.abc
import kubernetes
import kubernetes as k8s
import pendulum
import pytest
import yaml
from jsonpatch import JsonPatch
from kubernetes.config import ConfigException
from pydantic import ValidationError

from prefect.infrastructure.kubernetes import (
    KubernetesImagePullPolicy,
    KubernetesJob,
    KubernetesManifest,
)


@pytest.fixture(autouse=True)
def skip_if_kubernetes_is_not_installed():
    pytest.importorskip("kubernetes")


@pytest.fixture
def mock_watch(monkeypatch):
    pytest.importorskip("kubernetes")

    mock = MagicMock()

    monkeypatch.setattr("kubernetes.watch.Watch", MagicMock(return_value=mock))
    return mock


@pytest.fixture
def mock_cluster_config(monkeypatch):
    pytest.importorskip("kubernetes")

    mock = MagicMock()
    # We cannot mock this or the `except` clause will complain
    mock.config.ConfigException = ConfigException

    monkeypatch.setattr("kubernetes.config", mock)
    monkeypatch.setattr("kubernetes.config.ConfigException", ConfigException)
    return mock


@pytest.fixture
def mock_k8s_v1_job():
    mock = MagicMock(spec=k8s.client.V1Job)
    mock.metadata.name = "mock-k8s-v1-job"
    return mock


@pytest.fixture
def mock_k8s_client(monkeypatch, mock_cluster_config):
    pytest.importorskip("kubernetes")

    mock = MagicMock(spec=k8s.client.CoreV1Api)

    @contextmanager
    def get_client(_):
        yield mock

    monkeypatch.setattr(
        "prefect.infrastructure.kubernetes.KubernetesJob.get_client",
        get_client,
    )
    return mock


@pytest.fixture
def mock_k8s_batch_client(monkeypatch, mock_cluster_config, mock_k8s_v1_job):
    pytest.importorskip("kubernetes")

    mock = MagicMock(spec=k8s.client.BatchV1Api)
    mock.read_namespaced_job.return_value = mock_k8s_v1_job
    mock.create_namespaced_job.return_value = mock_k8s_v1_job

    @contextmanager
    def get_batch_client(_):
        yield mock

    monkeypatch.setattr(
        "prefect.infrastructure.kubernetes.KubernetesJob.get_batch_client",
        get_batch_client,
    )
    return mock


def _mock_pods_stream_that_returns_running_pod(*args, **kwargs):
    job_pod = MagicMock(spec=kubernetes.client.V1Pod)
    job_pod.status.phase = "Running"

    job = MagicMock(spec=kubernetes.client.V1Job)
    job.status.completion_time = pendulum.now("utc").timestamp()

    return [{"object": job_pod}, {"object": job}]


def test_infrastructure_type():
    assert KubernetesJob().type == "kubernetes-job"


def test_building_a_job_is_idempotent():
    """Building a Job twice from should return different copies
    of the Job manifest with identical values"""
    k8s_job = KubernetesJob(command=["echo", "hello"])
    first_time = k8s_job.build_job()
    second_time = k8s_job.build_job()
    assert first_time is not second_time
    assert first_time == second_time


async def test_creates_job_by_building_a_manifest(
    mock_k8s_batch_client,
    mock_k8s_client,
    mock_watch,
):
    mock_watch.stream = _mock_pods_stream_that_returns_running_pod

    fake_status = MagicMock(spec=anyio.abc.TaskStatus)
    k8s_job = KubernetesJob(command=["echo", "hello"])
    expected_manifest = k8s_job.build_job()
    await k8s_job.run(fake_status)
    mock_k8s_client.read_namespaced_pod_status.assert_called_once()

    mock_k8s_batch_client.create_namespaced_job.assert_called_with(
        "default",
        expected_manifest,
    )

    fake_status.started.assert_called_once()


async def test_task_status_receives_job_name(
    mock_k8s_batch_client,
    mock_k8s_client,
    mock_watch,
):
    fake_status = MagicMock(spec=anyio.abc.TaskStatus)
    result = await KubernetesJob(command=["echo", "hello"]).run(task_status=fake_status)
    fake_status.started.assert_called_once_with(result.identifier)


@pytest.mark.parametrize(
    "job_name,clean_name",
    [
        ("_infra_run", "infra-run"),
        ("...infra_run", "infra-run"),
        ("._-infra_run", "infra-run"),
        ("9infra-run", "9infra-run"),
        ("-infra.run", "infra-run"),
        ("infra*run", "infra-run"),
        ("infra9.-foo_bar^x", "infra9-foo-bar-x"),
    ],
)
async def test_job_name_creates_valid_name(
    mock_k8s_client,
    mock_watch,
    mock_k8s_batch_client,
    job_name,
    clean_name,
):
    mock_watch.stream = _mock_pods_stream_that_returns_running_pod
    fake_status = MagicMock(spec=anyio.abc.TaskStatus)
    await KubernetesJob(name=job_name, command=["echo", "hello"]).run(fake_status)
    mock_k8s_batch_client.create_namespaced_job.assert_called_once()
    call_name = mock_k8s_batch_client.create_namespaced_job.call_args[0][1]["metadata"][
        "generateName"
    ]
    assert call_name == clean_name


async def test_uses_image_setting(
    mock_k8s_client,
    mock_watch,
    mock_k8s_batch_client,
):
    mock_watch.stream = _mock_pods_stream_that_returns_running_pod

    await KubernetesJob(command=["echo", "hello"], image="foo").run(MagicMock())
    mock_k8s_batch_client.create_namespaced_job.assert_called_once()
    image = mock_k8s_batch_client.create_namespaced_job.call_args[0][1]["spec"][
        "template"
    ]["spec"]["containers"][0]["image"]
    assert image == "foo"


async def test_uses_labels_setting(
    mock_k8s_client,
    mock_watch,
    mock_k8s_batch_client,
):
    mock_watch.stream = _mock_pods_stream_that_returns_running_pod

    await KubernetesJob(
        command=["echo", "hello"], labels={"foo": "foo", "bar": "bar"}
    ).run(MagicMock())
    mock_k8s_batch_client.create_namespaced_job.assert_called_once()
    labels = mock_k8s_batch_client.create_namespaced_job.call_args[0][1]["metadata"][
        "labels"
    ]
    assert labels["foo"] == "foo"
    assert labels["bar"] == "bar"


@pytest.mark.parametrize(
    "given,expected",
    [
        ("a-valid-dns-subdomain1/and-a-name", "a-valid-dns-subdomain1/and-a-name"),
        (
            "a-prefix-with-invalid$@*^$@-characters/and-a-name",
            "a-prefix-with-invalid-characters/and-a-name",
        ),
        (
            "a-name-with-invalid$@*^$@-characters",
            "a-name-with-invalid-characters",
        ),
        ("/a-name-that-starts-with-slash", "a-name-that-starts-with-slash"),
        ("a-prefix/and-a-name/-with-a-slash", "a-prefix/and-a-name-with-a-slash"),
        # Truncation of the prefix
        ("a" * 300 + "/and-a-name", "a" * 253 + "/and-a-name"),
        # Truncation of the name
        ("a" * 300, "a" * 63),
        # Truncation of the prefix and name together
        ("a" * 300 + "/" + "b" * 100, "a" * 253 + "/" + "b" * 63),
        # All invalid passes through
        ("$@*^$@", "$@*^$@"),
        # All invalid passes through for prefix
        ("$@*^$@/name", "$@*^$@/name"),
    ],
)
async def test_sanitizes_user_label_keys(
    mock_k8s_client,
    mock_watch,
    mock_k8s_batch_client,
    given,
    expected,
):
    mock_watch.stream = _mock_pods_stream_that_returns_running_pod

    await KubernetesJob(command=["echo", "hello"], labels={given: "foo"}).run(
        MagicMock()
    )
    mock_k8s_batch_client.create_namespaced_job.assert_called_once()
    labels = mock_k8s_batch_client.create_namespaced_job.call_args[0][1]["metadata"][
        "labels"
    ]
    assert len(list(labels.keys())) == 1, "Only a single label should be created"
    assert list(labels.keys())[0] == expected
    assert labels[expected] == "foo"


@pytest.mark.parametrize(
    "given,expected",
    [
        ("valid-label-text", "valid-label-text"),
        (
            "text-with-invalid$@*^$@-characters",
            "text-with-invalid-characters",
        ),
        # Truncation
        ("a" * 100, "a" * 63),
        # All invalid passes through
        ("$@*^$@", "$@*^$@"),
    ],
)
async def test_sanitizes_user_label_values(
    mock_k8s_client,
    mock_watch,
    mock_k8s_batch_client,
    given,
    expected,
):
    mock_watch.stream = _mock_pods_stream_that_returns_running_pod

    await KubernetesJob(command=["echo", "hello"], labels={"foo": given}).run(
        MagicMock()
    )
    mock_k8s_batch_client.create_namespaced_job.assert_called_once()
    labels = mock_k8s_batch_client.create_namespaced_job.call_args[0][1]["metadata"][
        "labels"
    ]
    assert labels["foo"] == expected


async def test_uses_namespace_setting(
    mock_k8s_client,
    mock_watch,
    mock_k8s_batch_client,
):
    mock_watch.stream = _mock_pods_stream_that_returns_running_pod

    await KubernetesJob(command=["echo", "hello"], namespace="foo").run(MagicMock())
    mock_k8s_batch_client.create_namespaced_job.assert_called_once()
    namespace = mock_k8s_batch_client.create_namespaced_job.call_args[0][1]["metadata"][
        "namespace"
    ]
    assert namespace == "foo"


async def test_uses_service_account_name_setting(
    mock_k8s_client,
    mock_watch,
    mock_k8s_batch_client,
):
    mock_watch.stream = _mock_pods_stream_that_returns_running_pod

    await KubernetesJob(command=["echo", "hello"], service_account_name="foo").run(
        MagicMock()
    )
    mock_k8s_batch_client.create_namespaced_job.assert_called_once()
    service_account_name = mock_k8s_batch_client.create_namespaced_job.call_args[0][1][
        "spec"
    ]["template"]["spec"]["serviceAccountName"]
    assert service_account_name == "foo"


async def test_defaults_to_unspecified_image_pull_policy(
    mock_k8s_client,
    mock_watch,
    mock_k8s_batch_client,
):
    mock_watch.stream = _mock_pods_stream_that_returns_running_pod

    await KubernetesJob(command=["echo", "hello"]).run(MagicMock())
    mock_k8s_batch_client.create_namespaced_job.assert_called_once()
    call_image_pull_policy = mock_k8s_batch_client.create_namespaced_job.call_args[0][
        1
    ]["spec"]["template"]["spec"]["containers"][0].get("imagePullPolicy")
    assert call_image_pull_policy is None


async def test_uses_specified_image_pull_policy(
    mock_k8s_client,
    mock_watch,
    mock_k8s_batch_client,
):
    mock_watch.stream = _mock_pods_stream_that_returns_running_pod

    await KubernetesJob(
        command=["echo", "hello"],
        image_pull_policy=KubernetesImagePullPolicy.IF_NOT_PRESENT,
    ).run(MagicMock())
    mock_k8s_batch_client.create_namespaced_job.assert_called_once()
    call_image_pull_policy = mock_k8s_batch_client.create_namespaced_job.call_args[0][
        1
    ]["spec"]["template"]["spec"]["containers"][0].get("imagePullPolicy")
    assert call_image_pull_policy == "IfNotPresent"


async def test_defaults_to_unspecified_restart_policy(
    mock_k8s_client,
    mock_watch,
    mock_k8s_batch_client,
):
    mock_watch.stream = _mock_pods_stream_that_returns_running_pod

    await KubernetesJob(command=["echo", "hello"]).run(MagicMock())
    mock_k8s_batch_client.create_namespaced_job.assert_called_once()
    call_restart_policy = mock_k8s_batch_client.create_namespaced_job.call_args[0][1][
        "spec"
    ]["template"]["spec"].get("imagePullPolicy")
    assert call_restart_policy is None


async def test_no_raise_on_submission_with_hosted_api(
    mock_cluster_config,
    mock_k8s_batch_client,
    mock_k8s_client,
    use_hosted_orion,
):
    await KubernetesJob(command=["echo", "hello"]).run(MagicMock())


async def test_defaults_to_incluster_config(
    mock_k8s_client,
    mock_watch,
    mock_cluster_config,
    mock_k8s_batch_client,
):
    mock_watch.stream = _mock_pods_stream_that_returns_running_pod
    fake_status = MagicMock(spec=anyio.abc.TaskStatus)

    await KubernetesJob(command=["echo", "hello"]).run(fake_status)

    mock_cluster_config.load_incluster_config.assert_called_once()
    assert not mock_cluster_config.load_kube_config.called


async def test_uses_cluster_config_if_not_in_cluster(
    mock_k8s_client,
    mock_watch,
    mock_cluster_config,
    mock_k8s_batch_client,
):
    mock_watch.stream = _mock_pods_stream_that_returns_running_pod
    fake_status = MagicMock(spec=anyio.abc.TaskStatus)

    mock_cluster_config.load_incluster_config.side_effect = ConfigException()

    await KubernetesJob(command=["echo", "hello"]).run(fake_status)

    mock_cluster_config.load_kube_config.assert_called_once()


async def test_allows_configurable_timeouts_for_pod_and_job_watches(
    mock_k8s_client,
    mock_watch,
    mock_k8s_batch_client,
):
    mock_watch.stream = mock.Mock(
        side_effect=_mock_pods_stream_that_returns_running_pod
    )

    await KubernetesJob(
        command=["echo", "hello"],
        pod_watch_timeout_seconds=42,
        job_watch_timeout_seconds=24,
    ).run(MagicMock())

    mock_watch.stream.assert_has_calls(
        [
            mock.call(
                func=mock_k8s_client.list_namespaced_pod,
                namespace=mock.ANY,
                label_selector=mock.ANY,
                timeout_seconds=42,
            ),
            mock.call(
                func=mock_k8s_batch_client.list_namespaced_job,
                namespace=mock.ANY,
                field_selector=mock.ANY,
                timeout_seconds=24,
            ),
        ]
    )


async def test_watches_the_right_namespace(
    mock_k8s_client,
    mock_watch,
    mock_k8s_batch_client,
):
    mock_watch.stream = mock.Mock(
        side_effect=_mock_pods_stream_that_returns_running_pod
    )

    await KubernetesJob(command=["echo", "hello"], namespace="my-awesome-flows").run(
        MagicMock()
    )

    mock_watch.stream.assert_has_calls(
        [
            mock.call(
                func=mock_k8s_client.list_namespaced_pod,
                namespace="my-awesome-flows",
                label_selector=mock.ANY,
                timeout_seconds=mock.ANY,
            ),
            mock.call(
                func=mock_k8s_batch_client.list_namespaced_job,
                namespace="my-awesome-flows",
                field_selector=mock.ANY,
                timeout_seconds=mock.ANY,
            ),
        ]
    )


class TestCustomizingBaseJob:
    """Tests scenarios where a user is providing a customized base Job template"""

    def test_validates_against_an_empty_job(self):
        """We should give a human-friendly error when the user provides an empty custom
        Job manifest"""
        with pytest.raises(ValidationError) as excinfo:
            KubernetesJob(job={})

        assert excinfo.value.errors() == [
            {
                "loc": ("job",),
                "msg": (
                    "Job is missing required attributes at the following paths: "
                    "/apiVersion, /kind, /metadata, /spec"
                ),
                "type": "value_error",
            }
        ]

    def test_validates_for_a_job_missing_deeper_attributes(self):
        """We should give a human-friendly error when the user provides an incomplete
        custom Job manifest"""
        with pytest.raises(ValidationError) as excinfo:
            KubernetesJob(
                command=["echo", "hello"],
                job={
                    "apiVersion": "batch/v1",
                    "kind": "Job",
                    "metadata": {},
                    "spec": {"template": {"spec": {}}},
                },
            )

        assert excinfo.value.errors() == [
            {
                "loc": ("job",),
                "msg": (
                    "Job is missing required attributes at the following paths: "
                    "/metadata/labels, /spec/template/spec/completions, "
                    "/spec/template/spec/containers, "
                    "/spec/template/spec/parallelism, "
                    "/spec/template/spec/restartPolicy"
                ),
                "type": "value_error",
            }
        ]

    def test_validates_for_a_job_with_incompatible_values(self):
        """We should give a human-friendly error when the user provides a custom Job
        manifest that is attempting to change required values."""
        with pytest.raises(ValidationError) as excinfo:
            KubernetesJob(
                command=["echo", "hello"],
                job={
                    "apiVersion": "v1",
                    "kind": "JobbledyJunk",
                    "metadata": {"labels": {}},
                    "spec": {
                        "template": {
                            "spec": {
                                "parallelism": 1,
                                "completions": 1,
                                "restartPolicy": "Never",
                                "containers": [
                                    {
                                        "name": "prefect-job",
                                        "env": [],
                                    }
                                ],
                            }
                        }
                    },
                },
            )

        assert excinfo.value.errors() == [
            {
                "loc": ("job",),
                "msg": (
                    "Job has incompatble values for the following attributes: "
                    "/apiVersion must have value 'batch/v1', "
                    "/kind must have value 'Job'"
                ),
                "type": "value_error",
            }
        ]

    def test_user_supplied_base_job_with_labels(self):
        """The user can supply a custom base job with labels and they will be
        included in the final manifest"""
        manifest = KubernetesJob(
            command=["echo", "hello"],
            job={
                "apiVersion": "batch/v1",
                "kind": "Job",
                "metadata": {"labels": {"my-custom-label": "sweet"}},
                "spec": {
                    "template": {
                        "spec": {
                            "parallelism": 1,
                            "completions": 1,
                            "restartPolicy": "Never",
                            "containers": [
                                {
                                    "name": "prefect-job",
                                    "env": [],
                                }
                            ],
                        }
                    }
                },
            },
        ).build_job()

        assert manifest["metadata"]["labels"] == {
            # the labels provided in the user's job base
            "my-custom-label": "sweet",
        }

    def test_user_can_supply_a_sidecar_container_and_volume(self):
        """The user can supply a custom base job that includes more complex
        modifications, like a sidecar container and volumes"""
        manifest = KubernetesJob(
            command=["echo", "hello"],
            job={
                "apiVersion": "batch/v1",
                "kind": "Job",
                "metadata": {"labels": {}},
                "spec": {
                    "template": {
                        "spec": {
                            "parallelism": 1,
                            "completions": 1,
                            "restartPolicy": "Never",
                            "containers": [
                                {
                                    "name": "prefect-job",
                                    "env": [],
                                },
                                {
                                    "name": "my-sidecar",
                                    "image": "cool-peeps/cool-code:latest",
                                    "volumeMounts": [
                                        {"name": "data-volume", "mountPath": "/data/"}
                                    ],
                                },
                            ],
                            "volumes": [
                                {"name": "data-volume", "hostPath": "/all/the/data/"}
                            ],
                        }
                    }
                },
            },
        ).build_job()

        pod = manifest["spec"]["template"]["spec"]

        assert pod["volumes"] == [{"name": "data-volume", "hostPath": "/all/the/data/"}]

        # the prefect-job container is still populated
        assert pod["containers"][0]["name"] == "prefect-job"
        assert pod["containers"][0]["command"] == ["echo", "hello"]

        assert pod["containers"][1] == {
            "name": "my-sidecar",
            "image": "cool-peeps/cool-code:latest",
            "volumeMounts": [{"name": "data-volume", "mountPath": "/data/"}],
        }


class TestCustomizingJob:
    """Tests scenarios where the user is providing targeted RFC 6902 JSON patches to
    customize their Job"""

    @staticmethod
    def find_environment_variable(manifest: KubernetesManifest, name: str) -> Dict:
        pod = manifest["spec"]["template"]["spec"]
        env = pod["containers"][0]["env"]
        for variable in env:
            if variable["name"] == name:
                return variable
        assert False, f"{name} not found in pod environment variables: {env!r}"

    def test_providing_a_secret_key_as_an_environment_variable(self):
        manifest = KubernetesJob(
            command=["echo", "hello"],
            customizations=[
                {
                    "op": "add",
                    "path": "/spec/template/spec/containers/0/env/-",
                    "value": {
                        "name": "MY_API_TOKEN",
                        "valueFrom": {
                            "secretKeyRef": {
                                "name": "the-secret-name",
                                "key": "api-token",
                            }
                        },
                    },
                }
            ],
        ).build_job()

        variable = self.find_environment_variable(manifest, "MY_API_TOKEN")
        assert variable == {
            "name": "MY_API_TOKEN",
            "valueFrom": {
                "secretKeyRef": {
                    "name": "the-secret-name",
                    "key": "api-token",
                }
            },
        }

    def test_setting_pod_resource_requests(self):
        manifest = KubernetesJob(
            command=["echo", "hello"],
            customizations=[
                {
                    "op": "add",
                    "path": "/spec/template/spec/resources",
                    "value": {"limits": {"memory": "8Gi", "cpu": "4000m"}},
                }
            ],
        ).build_job()

        pod = manifest["spec"]["template"]["spec"]
        assert pod["resources"]["limits"] == {
            "memory": "8Gi",
            "cpu": "4000m",
        }

        # prefect's orchestration values are still there
        assert pod["completions"] == 1

    def test_requesting_a_fancy_gpu(self):
        manifest = KubernetesJob(
            command=["echo", "hello"],
            customizations=[
                {
                    "op": "add",
                    "path": "/spec/template/spec/resources",
                    "value": {"limits": {}},
                },
                {
                    "op": "add",
                    "path": "/spec/template/spec/resources/limits",
                    "value": {"nvidia.com/gpu": 2},
                },
                {
                    "op": "add",
                    "path": "/spec/template/spec/nodeSelector",
                    "value": {"cloud.google.com/gke-accelerator": "nvidia-tesla-k80"},
                },
            ],
        ).build_job()

        pod = manifest["spec"]["template"]["spec"]
        assert pod["resources"]["limits"] == {
            "nvidia.com/gpu": 2,
        }
        assert pod["nodeSelector"] == {
            "cloud.google.com/gke-accelerator": "nvidia-tesla-k80",
        }

        # prefect's orchestration values are still there
        assert pod["completions"] == 1

    def test_label_with_slash_in_it(self):
        """Documenting the use of ~1 to stand in for a /, according to RFC 6902"""
        manifest = KubernetesJob(
            command=["echo", "hello"],
            customizations=[
                {
                    "op": "add",
                    "path": "/metadata/labels/example.com~1a-cool-key",
                    "value": "hi!",
                }
            ],
        ).build_job()

        labels = manifest["metadata"]["labels"]
        assert labels["example.com/a-cool-key"] == "hi!"

    def test_user_overriding_command_line(self):
        """Users should be able to wrap the command-line with another command"""
        manifest = KubernetesJob(
            command=["echo", "hello"],
            customizations=[
                {
                    "op": "add",
                    "path": "/spec/template/spec/containers/0/command/0",
                    "value": "opentelemetry-instrument",
                },
                {
                    "op": "add",
                    "path": "/spec/template/spec/containers/0/command/1",
                    "value": "--resource_attributes",
                },
                {
                    "op": "add",
                    "path": "/spec/template/spec/containers/0/command/2",
                    "value": "service.name=my-cool-job",
                },
            ],
        ).build_job()

        assert manifest["spec"]["template"]["spec"]["containers"][0]["command"] == [
            "opentelemetry-instrument",
            "--resource_attributes",
            "service.name=my-cool-job",
            "echo",
            "hello",
        ]


class TestLoadingManifestsFromFiles:
    @pytest.fixture
    def example(self) -> KubernetesManifest:
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {"labels": {"my-custom-label": "sweet"}},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "prefect-job",
                                "env": [],
                            }
                        ]
                    }
                }
            },
        }

    @pytest.fixture
    def example_yaml(self, tmp_path: Path, example: KubernetesManifest) -> Path:
        filename = tmp_path / "example.yaml"
        with open(filename, "w") as f:
            yaml.dump(example, f)
        yield filename

    def test_job_from_yaml(self, example: KubernetesManifest, example_yaml: Path):
        assert KubernetesJob.job_from_file(example_yaml) == example

    @pytest.fixture
    def example_json(self, tmp_path: Path, example: KubernetesManifest) -> Path:
        filename = tmp_path / "example.json"
        with open(filename, "w") as f:
            json.dump(example, f)
        yield filename

    def test_job_from_json(self, example: KubernetesManifest, example_json: Path):
        assert KubernetesJob.job_from_file(example_json) == example


class TestLoadingPatchesFromFiles:
    def test_assumptions_about_jsonpatch(self):
        """Assert our assumptions about the behavior of the jsonpatch library, so we
        can be alert to any upstream changes"""
        patch_1 = JsonPatch([{"op": "add", "path": "/hi", "value": "there"}])
        patch_2 = JsonPatch([{"op": "add", "path": "/hi", "value": "there"}])
        patch_3 = JsonPatch([{"op": "add", "path": "/different", "value": "there"}])
        assert patch_1 is not patch_2
        assert patch_1 == patch_2
        assert patch_1 != patch_3

        assert list(patch_1) == list(patch_2)
        assert list(patch_1) != list(patch_3)

        assert patch_1.apply({}) == patch_2.apply({})
        assert patch_1.apply({}) != patch_3.apply({})

    @pytest.fixture
    def example(self) -> JsonPatch:
        return JsonPatch(
            [
                {
                    "op": "add",
                    "path": "/spec/template/spec/containers/0/env/-",
                    "value": {
                        "name": "MY_API_TOKEN",
                        "valueFrom": {
                            "secretKeyRef": {
                                "name": "the-secret-name",
                                "key": "api-token",
                            }
                        },
                    },
                },
                {
                    "op": "add",
                    "path": "/spec/template/spec/resources",
                    "value": {"limits": {"memory": "8Gi", "cpu": "4000m"}},
                },
            ]
        )

    @pytest.fixture
    def example_yaml(self, tmp_path: Path, example: JsonPatch) -> Path:
        filename = tmp_path / "example.yaml"
        with open(filename, "w") as f:
            yaml.dump(list(example), f)
        yield filename

    def test_patch_from_yaml(self, example: KubernetesManifest, example_yaml: Path):
        assert KubernetesJob.customize_from_file(example_yaml) == example

    @pytest.fixture
    def example_json(self, tmp_path: Path, example: JsonPatch) -> Path:
        filename = tmp_path / "example.json"
        with open(filename, "w") as f:
            json.dump(list(example), f)
        yield filename

    def test_patch_from_json(self, example: KubernetesManifest, example_json: Path):
        assert KubernetesJob.customize_from_file(example_json) == example
