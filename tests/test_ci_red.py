def test_deliberately_failing():
    assert 1 + 1 == 3  # throwaway: checks CI blocks the merge (#29)
