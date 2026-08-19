import { describe, expect, it } from 'vitest'
import { analyzeVault, MIN_CLUSTER_SIZE, type AnalysisEdge, type AnalysisNode } from './vaultAnalysis'

/** Two 4-cliques joined by a single edge through `bridge`. */
function twoClusterGraph(): { nodes: AnalysisNode[]; edges: AnalysisEdge[] } {
  const left = ['l1', 'l2', 'l3', 'bridge']
  const right = ['r1', 'r2', 'r3', 'r4']
  const edges: AnalysisEdge[] = []
  for (const group of [left, right]) {
    for (let i = 0; i < group.length; i++) {
      for (let j = i + 1; j < group.length; j++) edges.push({ source: group[i], target: group[j] })
    }
  }
  edges.push({ source: 'bridge', target: 'r1' })
  const degree = new Map<string, number>()
  for (const e of edges) {
    degree.set(e.source, (degree.get(e.source) || 0) + 1)
    degree.set(e.target, (degree.get(e.target) || 0) + 1)
  }
  const nodes = [...left, ...right].map(id => ({ id, title: id.toUpperCase(), degree: degree.get(id) || 0 }))
  return { nodes, edges }
}

describe('analyzeVault', () => {
  it('returns empty analysis for an empty vault', () => {
    const out = analyzeVault([], [])
    expect(out.clusters).toEqual([])
    expect(out.communityOf.size).toBe(0)
    expect(out.betweenness.size).toBe(0)
  })

  it('separates two cliques into two clusters', () => {
    const { nodes, edges } = twoClusterGraph()
    const out = analyzeVault(nodes, edges)
    expect(out.clusters).toHaveLength(2)
    const left = out.communityOf.get('l1')
    expect(out.communityOf.get('l2')).toBe(left)
    expect(out.communityOf.get('l3')).toBe(left)
    expect(out.communityOf.get('r2')).not.toBe(left)
  })

  it('assigns slots by size, largest first', () => {
    const nodes: AnalysisNode[] = []
    const edges: AnalysisEdge[] = []
    // A 6-clique and a 3-clique, deliberately declared smallest-first so the
    // ordering cannot come from input order.
    const small = ['s1', 's2', 's3']
    const big = ['b1', 'b2', 'b3', 'b4', 'b5', 'b6']
    for (const group of [small, big]) {
      for (let i = 0; i < group.length; i++) {
        for (let j = i + 1; j < group.length; j++) edges.push({ source: group[i], target: group[j] })
      }
    }
    for (const id of [...small, ...big]) nodes.push({ id, title: id, degree: id[0] === 's' ? 2 : 5 })
    const out = analyzeVault(nodes, edges)
    expect(out.clusters[0].slot).toBe(0)
    expect(out.clusters[0].size).toBe(6)
    expect(out.clusters[1].size).toBe(3)
  })

  it('is deterministic across runs, so cluster colours never shuffle', () => {
    const { nodes, edges } = twoClusterGraph()
    const a = analyzeVault(nodes, edges)
    const b = analyzeVault(nodes, edges)
    expect(a.clusters.map(c => [c.slot, c.label, c.size])).toEqual(b.clusters.map(c => [c.slot, c.label, c.size]))
    expect([...a.communityOf.entries()].sort()).toEqual([...b.communityOf.entries()].sort())
  })

  it('names a cluster after its most-connected member', () => {
    const nodes: AnalysisNode[] = [
      { id: 'a', title: 'Alpha', degree: 1 },
      { id: 'b', title: 'Beta', degree: 1 },
      { id: 'hub', title: 'The Hub', degree: 2 },
    ]
    const edges: AnalysisEdge[] = [
      { source: 'hub', target: 'a' },
      { source: 'hub', target: 'b' },
    ]
    const out = analyzeVault(nodes, edges)
    expect(out.clusters).toHaveLength(1)
    expect(out.clusters[0].label).toBe('The Hub')
  })

  it('scores a bridge note above the cliques it joins', () => {
    const { nodes, edges } = twoClusterGraph()
    const out = analyzeVault(nodes, edges)
    const bridge = out.betweenness.get('bridge') || 0
    // `bridge` sits on every shortest path between the two cliques; a plain
    // clique member sits on none of them. Degree alone cannot tell them apart
    // (bridge has 4 links, l1 has 3), which is the point of the metric.
    expect(bridge).toBeGreaterThan(out.betweenness.get('l1') || 0)
    expect(bridge).toBeGreaterThan(out.betweenness.get('r2') || 0)
  })

  it('drops communities below the minimum size and leaves them unassigned', () => {
    const nodes: AnalysisNode[] = [
      { id: 'p1', title: 'Pair one', degree: 1 },
      { id: 'p2', title: 'Pair two', degree: 1 },
      { id: 'orphan', title: 'Orphan', degree: 0 },
    ]
    const out = analyzeVault(nodes, [{ source: 'p1', target: 'p2' }])
    expect(MIN_CLUSTER_SIZE).toBe(3)
    expect(out.clusters).toEqual([])
    expect(out.communityOf.has('p1')).toBe(false)
    expect(out.communityOf.has('orphan')).toBe(false)
  })

  it('ignores self-loops, duplicate edges, and edges to unknown nodes', () => {
    const nodes: AnalysisNode[] = [
      { id: 'a', title: 'A', degree: 2 },
      { id: 'b', title: 'B', degree: 2 },
      { id: 'c', title: 'C', degree: 2 },
    ]
    const edges: AnalysisEdge[] = [
      { source: 'a', target: 'b' },
      { source: 'b', target: 'a' },
      { source: 'a', target: 'a' },
      { source: 'b', target: 'c' },
      { source: 'c', target: 'a' },
      { source: 'a', target: 'gone' },
    ]
    expect(() => analyzeVault(nodes, edges)).not.toThrow()
    const out = analyzeVault(nodes, edges)
    expect(out.clusters).toHaveLength(1)
    expect(out.clusters[0].size).toBe(3)
  })
})
