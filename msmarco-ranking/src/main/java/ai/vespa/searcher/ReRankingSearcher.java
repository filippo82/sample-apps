// Copyright Verizon Media. Licensed under the terms of the Apache 2.0 license. See LICENSE in the project root.
package ai.vespa.searcher;

import ai.vespa.models.evaluation.FunctionEvaluator;
import ai.vespa.models.evaluation.Model;
import ai.vespa.models.evaluation.ModelsEvaluator;
import com.yahoo.search.Query;
import com.yahoo.search.Result;
import com.yahoo.search.Searcher;
import com.yahoo.search.result.Hit;
import com.yahoo.search.searchchain.Execution;
import com.yahoo.tensor.IndexedTensor;
import com.yahoo.tensor.Tensor;
import com.yahoo.tensor.TensorAddress;
import com.yahoo.tensor.TensorType;

import java.util.ArrayList;
import java.util.List;
import java.util.ListIterator;

public class ReRankingSearcher extends Searcher {

    private final Model model;
    private static final String MODEL_NAME = "msmarco_v2";


    protected static class BertModelBatchInput  {
        IndexedTensor inputIds;
        IndexedTensor attentionMask;
        IndexedTensor tokenTypeIds;
        List<Hit> hits;

        BertModelBatchInput(IndexedTensor inputIds, IndexedTensor attentionMask, IndexedTensor tokenTypeIds,
                       List<Hit> hits)  {
            this.inputIds = inputIds;
            this.attentionMask = attentionMask;
            this.tokenTypeIds = tokenTypeIds;
            this.hits = hits;
        }
    }

    public ReRankingSearcher(ModelsEvaluator modelsEvaluator) {
        this.model = modelsEvaluator.requireModel(MODEL_NAME);
    }

    @Override
    public Result search(Query query, Execution execution) {
        int hits = query.getHits();
        int reRankCount = query.getRanking().getRerankCount();
        query.setHits(reRankCount);
        query.getPresentation().getSummaryFields().add("text_token_ids");
        Result result = execution.search(query);
        execution.fill(result, "text_token_ids");
        Result reRanked = reRank(result);
        reRanked.hits().trim(0,hits);
        return reRanked;
    }

    private Result reRank(Result result) {
        if(result.getConcreteHitCount() == 0)
            return result;
        List<Integer> queryTokens = QueryTensorInput.getFrom(result.getQuery().properties()).getQueryTokenIds();
        int maxSequenceLength = result.getQuery().properties().getInteger("rerank.sequence-length", 128);

        long start = System.currentTimeMillis();
        BertModelBatchInput input = buildModelInput(queryTokens, result,maxSequenceLength);
        if(result.getQuery().isTraceable(1))
            result.getQuery().trace("Prepare batch input took " + (System.currentTimeMillis() - start)  + " ms",1);

        start = System.currentTimeMillis();
        batchInference(input);
        if(result.getQuery().isTraceable(1))
            result.getQuery().trace("Inference batch took " + (System.currentTimeMillis() - start)  + " ms",1);
        result.hits().sort();
        return result;
    }

    protected static List<Integer> toList(Tensor t) {
        int size = (int)t.size();
        List<Integer> tokens = new ArrayList<>(size);
        for(int i = 0; i < size; i++) {
            double value = t.get(TensorAddress.of(i));
            if(value > 0)
                tokens.add((int)value);
        }
        return tokens;
    }


    protected static BertModelBatchInput buildModelInput(List<Integer> queryTokens, Result result,int maxSequenceLength) {

        List<List<Integer>> batch = new ArrayList<>(result.getHitCount());
        int maxPassageLength = 0;
        for (Hit h: result.hits()) {
            Tensor text = (Tensor) h.getField("text_token_ids");
            h.removeField("text_token_ids");
            List<Integer> textTokens = toList(text);
            batch.add(textTokens);
            if (textTokens.size() > maxPassageLength)
                maxPassageLength = textTokens.size();
        }

        int sequenceLength = maxSequenceLength + queryTokens.size() + 3;
        if(sequenceLength > maxSequenceLength)
            sequenceLength = maxSequenceLength;

        TensorType batchType = new TensorType.Builder(TensorType.Value.FLOAT).
                indexed("d0", result.hits().size()).indexed("d1",sequenceLength).build();
        IndexedTensor.Builder inputIdsBatchBuilder = IndexedTensor.Builder.of(batchType);
        IndexedTensor.Builder attentionMaskBatchBuilder = IndexedTensor.Builder.of(batchType);
        IndexedTensor.Builder tokenTypeIdsBatchBuilder = IndexedTensor.Builder.of(batchType);

        int batchId = 0;
        for (List<Integer> passage : batch) {
            int[] inputIds = new int[sequenceLength];
            byte[] attentionMask = new byte[sequenceLength];
            byte[] tokenType = new byte[sequenceLength];
            inputIds[0] = 101;
            attentionMask[0] = 1;
            tokenType[0] = 0;

            int index = 0;
            for (; index < queryTokens.size(); index++) {
                inputIds[index + 1] = queryTokens.get(index);
                attentionMask[index + 1] = 1;
                tokenType[index + 1] = 0;
            }
            inputIds[index + 1] = 102;
            attentionMask[index + 1] = 1;
            tokenType[index + 1] = 0;
            index++;
            for (int j = 0; j < passage.size() && index < maxSequenceLength -2; j++) {
                inputIds[index + 1] = passage.get(j);
                attentionMask[index + 1] = 1;
                tokenType[index + 1] = 1;
                index++;
            }
            inputIds[index + 1] = 102;
            attentionMask[index + 1] = 1;
            tokenType[index + 1] = 1;

            for (int k = 0; k < sequenceLength; k++) {
                inputIdsBatchBuilder.cell(inputIds[k], batchId, k);
                attentionMaskBatchBuilder.cell(attentionMask[k], batchId, k);
                tokenTypeIdsBatchBuilder.cell(tokenType[k], batchId, k);
            }
            batchId++;
        }
        return new BertModelBatchInput(inputIdsBatchBuilder.build(),
                attentionMaskBatchBuilder.build(),
                tokenTypeIdsBatchBuilder.build(),
                result.hits().asList());
    }

    protected void batchInference(BertModelBatchInput input) {
        FunctionEvaluator evaluator = this.model.evaluatorOf();
        Tensor scores = evaluator.bind("input_ids",input.inputIds)
                .bind("attention_mask", input.attentionMask).
                bind("token_type_ids",input.tokenTypeIds).evaluate();
        ListIterator<Hit> it = input.hits.listIterator();
        while(it.hasNext()) {
            int index = it.nextIndex();
            Hit h = it.next();
            h.setRelevance(scores.get(TensorAddress.of(index,0)));
        }
    }
}
