import enum

from flux_k8s.crd import DIRECTIVEBREAKDOWN_CRD


class AllocationStrategy(enum.Enum):

    PER_COMPUTE = "AllocatePerCompute"
    SINGLE_SERVER = "AllocateSingleServer"
    ACROSS_SERVERS = "AllocateAcrossServers"


PER_COMPUTE_TYPES = ("xfs", "gfs2", "raw")
LUSTRE_TYPES = ("ost", "mdt", "mgt")


def build_allocation_sets(allocation_sets, local_allocations, nodes_per_nnf):
    ret = []
    for allocation in allocation_sets:
        for nnf_name in local_allocations:
            if allocation["label"] in PER_COMPUTE_TYPES:
                alloc_size = int(
                    local_allocations[nnf_name]
                    * allocation["percentage_of_total"]
                    / nodes_per_nnf[nnf_name]
                )
                if alloc_size < allocation["minimumCapacity"]:
                    raise RuntimeError(
                        "Expected an allocation size of at least "
                        f"{allocation['minimumCapacity']}, got {alloc_size}"
                    )
                ret.append(
                    {
                        "allocationSize": alloc_size,
                        "label": allocation["label"],
                        "storage": [
                            {
                                "allocationCount": nodes_per_nnf[nnf_name],
                                "name": nnf_name,
                            }
                        ],
                    }
                )
            else:
                raise ValueError(f"{allocation['label']} not currently supported")
    return ret


def apply_breakdowns(k8s_api, workflow, resources):
    """Apply all of the directive breakdown information to a jobspec's `resources`."""
    breakdown_list = list(_fetch_breakdowns(k8s_api, workflow))
    per_compute_total = 0  # total bytes of per-compute storage
    for breakdown in breakdown_list:
        if breakdown["kind"] != "DirectiveBreakdown":
            raise ValueError(f"unsupported breakdown kind {breakdown['kind']!r}")
        if not breakdown["status"]["ready"]:
            raise RuntimeError("Breakdown marked as not ready")
        for allocation in breakdown["status"]["allocationSet"]:
            _apply_allocation(allocation, resources)
            # aggregate per-compute storage
            if allocation["label"] in PER_COMPUTE_TYPES:
                per_compute_total += allocation["minimumCapacity"]
    for breakdown in breakdown_list:
        for allocation in breakdown["status"]["allocationSet"]:
            if allocation["label"] in PER_COMPUTE_TYPES:
                allocation["percentage_of_total"] = (
                    allocation["minimumCapacity"] / per_compute_total
                )
    return breakdown_list


def _fetch_breakdowns(k8s_api, workflow):
    """Fetch all of the directive breakdowns associated with a workflow."""
    if not workflow["status"]["directiveBreakdowns"]:
        raise ValueError(f"workflow {workflow} has no directive breakdowns")
    for breakdown in workflow["status"]["directiveBreakdowns"]:
        yield k8s_api.get_namespaced_custom_object(
            DIRECTIVEBREAKDOWN_CRD.group,
            DIRECTIVEBREAKDOWN_CRD.version,
            breakdown["namespace"],
            DIRECTIVEBREAKDOWN_CRD.plural,
            breakdown["name"],
        )


def _apply_allocation(allocation, resources):
    """Parse a single 'allocationSet' and apply to it a jobspec's ``resources``."""
    expected_alloc_strats = {
        "xfs": AllocationStrategy.PER_COMPUTE.value,
        "raw": AllocationStrategy.PER_COMPUTE.value,
        "gfs2": AllocationStrategy.PER_COMPUTE.value,
        "ost": AllocationStrategy.ACROSS_SERVERS.value,
        "mdt": AllocationStrategy.SINGLE_SERVER.value,
        "mgt": AllocationStrategy.SINGLE_SERVER.value,
    }
    capacity_gb = max(1, allocation["minimumCapacity"] // (1024 ** 3))
    if allocation["allocationStrategy"] != expected_alloc_strats[allocation["label"]]:
        raise ValueError(
            f"{allocation['label']} allocationStrategy "
            f"must be {expected_alloc_strats[allocation['label']]!r} "
            f"but got {allocation['allocationStrategy']!r}"
        )
    if allocation["label"] in PER_COMPUTE_TYPES:
        _apply_alloc_per_compute(capacity_gb, resources)
    elif allocation["label"] in LUSTRE_TYPES:
        if allocation["label"] == "mgt":
            _apply_lustre(capacity_gb, resources)
    else:
        raise ValueError(f"Unknown label {allocation['label']!r}")


def _get_nnf_resource(capacity):
    return {
        "type": "nnf",
        "count": 1,
        "with": [{"type": "ssd", "count": capacity, "exclusive": True}],
    }


def _apply_alloc_per_compute(capacity, resources):
    """Apply XFS (node-local storage) to a jobspec's ``resources``."""
    if len(resources) == 2 and resources[1]["type"] == "nnf":
        resources[1]["with"][0]["count"] += capacity
    elif len(resources) > 1 or resources[0]["type"] != "node":
        raise ValueError("jobspec resources must have a single top-level 'node' entry")
    else:
        node = resources[0]
        nodecount = node["count"]
        resources.append(_get_nnf_resource(capacity))
        # node["count"] = 1
        # resources[0] = {"type": "slot", "label": "foobar", "count": nodecount, "with": [node, _get_nnf_resource(capacity)]}


def _apply_lustre(capacity, resources):
    """Apply Lustre OST/MGT/MDT to a jobspec's ``resources`` dictionary."""
    # if there is already a `rabbit-label[storage]` entry, add to its `count` field
    # for entry in resources:
    #     if entry["type"] == f"rabbit-{allocation['label']}":
    #         _aggregate_resources(entry["with"], allocation["minimumCapacity"])
    #         return
    resources.append(
        {"type": "globalnnf", "count": 1, "with": [_get_nnf_resource(capacity)],}
    )


def _aggregate_resources(with_resources, additional_capacity):
    for resource in with_resources:
        if resource["type"] == "storage":
            if resource.get("unit") == "B":
                resource["count"] = resource.get("count", 0) + additional_capacity
            else:
                raise ValueError(
                    f"Unit mismatch: expected 'B', got {resource.get('unit')}"
                )
            break
    else:
        raise ValueError(f"{entry} has no 'storage' entry")
