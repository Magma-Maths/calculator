from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_commit_images_use_full_commit_ids_and_released_tags_stay_immutable():
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    promote = (ROOT / ".github/workflows/promote.yml").read_text()

    assert ci.count("${{ env.IMAGE }}:sha-${{ github.sha }}") == 3
    assert "${GITHUB_SHA:0:7}" not in ci

    assert 'commit=$(git rev-parse "refs/tags/${GITHUB_REF_NAME}^{commit}")' in promote
    assert 'echo "commit=${commit}" >> "$GITHUB_OUTPUT"' in promote
    assert "${{ env.IMAGE }}:sha-${{ steps.rev.outputs.commit }}" in promote
    assert "${commit:0:7}" not in promote

    inspect_target = 'if out=$(docker buildx imagetools inspect "$TARGET"'
    create_target = 'docker buildx imagetools create --tag "$TARGET" "$SOURCE"'
    assert inspect_target in promote
    assert promote.index(inspect_target) < promote.index(create_target)
    release_guard = promote[promote.index(inspect_target):promote.index(create_target)]
    refusal = "already published; refusing to rebind a released version tag"
    assert refusal in release_guard
    assert release_guard.index(refusal) < release_guard.index("exit 1")
