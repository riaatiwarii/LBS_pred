from lbs_predictor.clustering import allocate_frvs_to_clusters


def test_allocate_frvs_gives_each_cluster_one_when_possible():
    allocation = allocate_frvs_to_clusters({1: 100, 2: 50, 3: 25}, total_frvs=5)

    assert sum(allocation.values()) == 5
    assert all(count >= 1 for count in allocation.values())


def test_allocate_frvs_prioritizes_biggest_clusters_when_fleet_is_short():
    allocation = allocate_frvs_to_clusters({1: 100, 2: 50, 3: 25}, total_frvs=2)

    assert allocation[1] == 1
    assert allocation[2] == 1
    assert allocation[3] == 0
