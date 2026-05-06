import pickle
graph = pickle.load(open("code_graph_cache/graph.pkl", "rb"))
for node in list(graph.nodes)[:10]:
    print(node.file_path)