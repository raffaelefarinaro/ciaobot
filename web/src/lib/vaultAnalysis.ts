import Graph from 'graphology'
import louvain from 'graphology-communities-louvain'
import betweenness from 'graphology-metrics/centrality/betweenness'

/**
 * Structural analysis of the vault graph: which notes clump into communities,
 * and which ones hold otherwise-separate clumps together.
 *
 * This exists because a global force layout of a real vault is not readable on
 * its own. Measured on a 318-note vault: median degree 2, 21% orphans, and
 * hubs at 65/62/50 links — so the picture is a hairball whose shape carries
 * almost no information. What people actually want out of a memory map is
 * "what are the themes in here", "what connects them", and "what did I write
 * that nothing links to", which are graph *metrics*, not pixels.
 *
 * Deliberately pure and free of Vue/Pinia so it can be unit-tested directly,
 * and computed against the FULL graph rather than the filtered view — a
 * category filter must never repaint the clusters that survive it.
 */

export interface AnalysisNode {
  id: string
  title: string
  degree: number
}
export interface AnalysisEdge {
  source: string
  target: string
}

export interface Cluster {
  /** Stable identity for this cluster across re-filters within one graph load. */
  id: number
  /**
   * Rank among clusters by size. Only the first COLORED_CLUSTERS slots get a
   * hue; everything past that shares the neutral "other" colour, because a
   * categorical palette may never invent a hue for an overflow series.
   */
  slot: number
  size: number
  /** Named after its most-connected member — the note a human would recognise. */
  label: string
  memberIds: string[]
}

export interface VaultAnalysis {
  /** node id -> cluster id; absent for nodes in no real cluster. */
  communityOf: Map<string, number>
  /** Real clusters only, largest first. */
  clusters: Cluster[]
  /** node id -> betweenness centrality (normalized). */
  betweenness: Map<string, number>
}

/**
 * A cluster of two notes is a pair, not a theme. Below this size a community
 * carries no summarising value and would just add noise to the legend — the
 * 67 orphans in a real vault each land in their own singleton community.
 */
export const MIN_CLUSTER_SIZE = 3

/**
 * Louvain is randomized, so without a fixed seed the clusters — and therefore
 * every cluster colour — would shuffle on each reload of the same vault.
 */
function seededRng(seed: number): () => number {
  let s = seed >>> 0
  return () => {
    s = (s + 0x6d2b79f5) >>> 0
    let t = s
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

export function analyzeVault(nodes: AnalysisNode[], edges: AnalysisEdge[]): VaultAnalysis {
  const empty: VaultAnalysis = { communityOf: new Map(), clusters: [], betweenness: new Map() }
  if (!nodes.length) return empty

  const graph = new Graph({ type: 'undirected' })
  for (const n of nodes) graph.addNode(n.id)
  for (const e of edges) {
    // The vault graph can carry an edge whose endpoint was filtered out of the
    // node list (cross-workspace links), and a duplicate would double-weight a
    // pair; graphology throws on both rather than ignoring them.
    if (e.source === e.target) continue
    if (!graph.hasNode(e.source) || !graph.hasNode(e.target)) continue
    if (graph.hasEdge(e.source, e.target)) continue
    graph.addEdge(e.source, e.target)
  }

  const communities = louvain(graph, { rng: seededRng(0x5eed) })
  const scores = betweenness(graph, { normalized: true })
  const betweennessMap = new Map<string, number>(Object.entries(scores))

  const byCommunity = new Map<number, string[]>()
  for (const [id, community] of Object.entries(communities)) {
    const bucket = byCommunity.get(community)
    if (bucket) bucket.push(id)
    else byCommunity.set(community, [id])
  }

  const degreeOf = new Map(nodes.map(n => [n.id, n.degree]))
  const titleOf = new Map(nodes.map(n => [n.id, n.title]))

  const real = [...byCommunity.entries()]
    .filter(([, members]) => members.length >= MIN_CLUSTER_SIZE)
    // Size descending, then by lowest member id: louvain's own community
    // numbering is arbitrary, so ordering has to come from the data to keep
    // colours stable between runs.
    .sort((a, b) => b[1].length - a[1].length || (a[1].slice().sort()[0] < b[1].slice().sort()[0] ? -1 : 1))

  const clusters: Cluster[] = real.map(([id, members], slot) => {
    const sorted = members
      .slice()
      .sort((a, b) => (degreeOf.get(b) || 0) - (degreeOf.get(a) || 0) || (a < b ? -1 : 1))
    return {
      id,
      slot,
      size: members.length,
      label: titleOf.get(sorted[0]) || sorted[0],
      memberIds: sorted,
    }
  })

  const communityOf = new Map<string, number>()
  for (const c of clusters) for (const id of c.memberIds) communityOf.set(id, c.id)

  return { communityOf, clusters, betweenness: betweennessMap }
}
