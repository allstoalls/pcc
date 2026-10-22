/* Isolated native selector ABI harness. No runtime archive or managed GC. */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include "harness_globals.h"
extern int64_t pcc_gc_backend4_select_relocation_pages(int64_t);
extern int64_t pcc_gc_backend4_candidate_fresh_skips_count(void);
extern int64_t pcc_gc_backend4_relocation_add_refusals_count(void);
extern void *__real_malloc(size_t);
extern void __real_free(void *);
static long pin_checks, polls, lock_depth, lock_errors, rank_allocs, rank_live, plan_live;
static long fail_rank, fail_plan, plan_attempts, threads, backend=4;
static unsigned char *objects, *nodes, *pages;
static long node_count,page_count,owners_per_page;
static uint64_t u64(void *p,int off){uint64_t v;memcpy(&v,(char*)p+off,8);return v;}
static void w64(void *p,int off,uint64_t v){memcpy((char*)p+off,&v,8);}
static uint32_t u32(void *p,int off){uint32_t v;memcpy(&v,(char*)p+off,4);return v;}
static void w32(void *p,int off,uint32_t v){memcpy((char*)p+off,&v,4);}
static void *ptr(void *p,int off){return (void*)(uintptr_t)u64(p,off);}
static void wp(void *p,int off,void *v){w64(p,off,(uintptr_t)v);}
static void *rank_ptr;
void *__wrap_malloc(size_t size){
 if(size!=16){rank_allocs++;if(fail_rank)return NULL;}
 else {plan_attempts++;if(fail_plan==plan_attempts)return NULL;}
 void *p=__real_malloc(size);
 if(p){if(size!=16){rank_ptr=p;rank_live++;}else plan_live++;}
 return p;
}
void __wrap_free(void *p){if(p){if(p==rank_ptr){rank_live--;rank_ptr=NULL;}else plan_live--;}__real_free(p);}
int64_t pcc_gc_object_is_address_pinned(void *p){pin_checks++;return (u32(p,12)&64)!=0;}
int64_t pcc_gc_backend4_relocate_copy_supported_tag(int64_t tag){return tag!=999;}
void pcc_gc_backend4_zpage_clear_active_page(void *p){(void)p;}
void *pcc_gc_forwarding_find(void *p){return u64(p,24)==1?p:NULL;}
int64_t pcc_gc_forwarding_target_exists(void *p){return u64(p,24)==2;}
void pcc_gc_relocation_member_note(void *p){(void)p;}
int64_t pcc_gc_config_ensure(void){return backend;}
void pcc_py_gc_minor_graph_lock(void){if(lock_depth++)lock_errors++;}
void pcc_py_gc_minor_graph_unlock(void){if(--lock_depth)lock_errors++;}
void pcc_thread_safepoint(void){polls++;if(lock_depth)lock_errors++;}
int64_t pcc_current_thread_id(void){return 1;}
int64_t pcc_threads_enabled(void){return threads;}
static uint64_t hash(uint64_t h,uint64_t x){return (h^x)*1099511628211ULL;}
static double now(void){struct timespec t;clock_gettime(CLOCK_MONOTONIC,&t);return t.tv_sec+t.tv_nsec*1e-9;}
int main(int argc,char **argv){
 if(argc!=8)return 2;
 const char *mode=argv[1];page_count=atol(argv[2]);owners_per_page=atol(argv[3]);
 long budget=atol(argv[4]);threads=atol(argv[5]);fail_rank=atol(argv[6]);fail_plan=atol(argv[7]);
 if(page_count<0||page_count>1024||owners_per_page<0||owners_per_page>128)return 3;
 node_count=page_count*owners_per_page;
 objects=calloc(node_count?node_count:1,256);nodes=calloc(node_count?node_count:1,80);pages=calloc(page_count?page_count:1,128);
 if(!objects||!nodes||!pages)return 4;
 for(long p=0;p<page_count;p++){
  void *page=pages+p*128;w64(page,8,1000);w64(page,16,5000);w32(page,24,0);w64(page,32,owners_per_page);
  if(strcmp(mode,"tie")&&strcmp(mode,"owner")){w64(page,40,(p*7)%19);w64(page,48,p%3);}
  if(!strcmp(mode,"mixed")&&p%3==0)w32(page,24,2);
  if(!strcmp(mode,"blocked")&&p%5==0)w64(page,88,1);
  if(!strcmp(mode,"preselected")&&p%3==0)w32(page,108,1);
  if(!strcmp(mode,"zero")){w64(page,8,5000);w64(page,40,0);w64(page,48,0);}
 }
 for(long i=0;i<node_count;i++){
  void *obj=objects+i*256,*node=nodes+i*80,*page=pages+(i%page_count)*128;
  w32(obj,8,2);w64(obj,16,i+1);wp(node,0,obj);wp(node,8,page);w64(node,32,64);
  wp(node,16,(void*)(uintptr_t)pcc_gc_backend4_zpage_head);pcc_gc_backend4_zpage_head=(uintptr_t)node;
  wp(node,48,ptr(page,112));wp(page,112,node);
  if(!strcmp(mode,"owner")&&i==0)w64(node,72,10000);
  if(!strcmp(mode,"mixed")){
   uint32_t flags=0;
   if(i%7==0)flags|=16384;if(i%11==0)flags|=64;if(i%17==0)flags|=524288;if(i%19==0)flags|=2048;if(i%5==0)flags|=256;
   w32(obj,12,flags);if(i%13==0)w32(obj,8,999);if(i%21==0)w64(obj,24,1);else if(i%23==0)w64(obj,24,2);
   if(i%29==0)w64(node,32,70000);w64(node,72,(i*17)%50);
  }
  if(!strcmp(mode,"fresh"))w32(obj,12,16384);
  if(!strcmp(mode,"forward"))w64(obj,24,1);
  if(!strcmp(mode,"residual")&&i%4==0)w32(obj,12,16384);
 }
 if(!strcmp(mode,"scanbusy"))pcc_gc_backend4_selector_scan_owner=77;
 if(!strcmp(mode,"pagebusy"))pcc_gc_backend4_selector_page_owner=77;
 if(!strcmp(mode,"resetbusy"))pcc_gc_backend4_relocation_reset_owner=77;
 if(!strcmp(mode,"reseedbusy"))pcc_gc_backend4_reseed_commit_owner=77;
 if(!strcmp(mode,"backend0"))backend=0;
 double start=now();int64_t selected=pcc_gc_backend4_select_relocation_pages(budget);int64_t second=0;
 if(!strcmp(mode,"reentry")){
  for(long p=0;p<page_count;p++)if(!u32(pages+p*128,108)){w64(pages+p*128,40,100000-p);break;}
  second=pcc_gc_backend4_select_relocation_pages(budget);
 }
 double elapsed=now()-start;
 uint64_t rh=1469598103934665603ULL,ph=rh,fh=rh;long reloc_n=0,page_n=0;long first_obj=-1,first_page=-1;
 for(void *r=(void*)(uintptr_t)pcc_gc_relocation_set_head;r;r=ptr(r,8)){
  long id=u64(ptr(r,0),16);rh=hash(rh,id);reloc_n++;first_obj=id;
 }
 for(void *r=(void*)(uintptr_t)pcc_gc_backend4_evacuation_page_head;r;r=ptr(r,8)){
  long id=((unsigned char*)ptr(r,0)-pages)/128;ph=hash(ph,id);page_n++;first_page=id;
 }
 for(long i=0;i<node_count;i++)fh=hash(fh,u32(objects+i*256,12));
 printf("{\"selected\":%lld,\"second\":%lld,\"reloc_n\":%ld,\"page_n\":%ld,\"reloc_hash\":%llu,\"page_hash\":%llu,\"flags_hash\":%llu,\"first_obj\":%ld,\"first_page\":%ld,",(long long)selected,(long long)second,reloc_n,page_n,(unsigned long long)rh,(unsigned long long)ph,(unsigned long long)fh,first_obj,first_page);
 printf("\"fresh\":%lld,\"refusals\":%lld,\"deferred\":%llu,\"deferred_bytes\":%llu,\"candidates\":%llu,\"candidate_bytes\":%llu,\"zpage_bytes\":%llu,\"revision\":%llu,\"page_revision\":%llu,",(long long)pcc_gc_backend4_candidate_fresh_skips_count(),(long long)pcc_gc_backend4_relocation_add_refusals_count(),(unsigned long long)pcc_gc_backend4_large_object_defers,(unsigned long long)pcc_gc_backend4_large_object_deferred_bytes_count,(unsigned long long)pcc_gc_backend4_evacuation_candidates,(unsigned long long)pcc_gc_backend4_evacuation_candidate_bytes_count,(unsigned long long)pcc_gc_backend4_evacuation_candidate_zpage_bytes_count,(unsigned long long)pcc_gc_backend4_reseed_relocation_revision,(unsigned long long)pcc_gc_backend4_reseed_page_revision);
 printf("\"small_candidates\":%llu,\"medium_candidates\":%llu,\"small_bytes\":%llu,\"medium_bytes\":%llu,\"small_zbytes\":%llu,\"medium_zbytes\":%llu,",(unsigned long long)pcc_gc_backend4_small_page_candidates,(unsigned long long)pcc_gc_backend4_medium_page_candidates,(unsigned long long)pcc_gc_backend4_small_page_candidate_bytes_count,(unsigned long long)pcc_gc_backend4_medium_page_candidate_bytes_count,(unsigned long long)pcc_gc_backend4_small_page_candidate_zpage_bytes_count,(unsigned long long)pcc_gc_backend4_medium_page_candidate_zpage_bytes_count);
 printf("\"pin_checks\":%ld,\"polls\":%ld,\"lock_errors\":%ld,\"rank_allocs\":%ld,\"rank_live\":%ld,\"plan_live\":%ld,\"seconds\":%.9f}\n",pin_checks,polls,lock_errors,rank_allocs,rank_live,plan_live,elapsed);
 return lock_errors||rank_live||plan_live!=reloc_n+page_n?5:0;
}
