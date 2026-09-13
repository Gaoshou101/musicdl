function handle(request) {
  return {hits: [{title: request.payload.query || ""}]};
}
